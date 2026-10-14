"""The board scan's HTTP surface, as a router so it can be mounted twice.

It runs on its own server today (`server_boardscan.py`, port 8788) and is
meant to be mounted into the main app later without changing a line here.
That is the whole reason this is an `APIRouter` and not a `FastAPI`.

Two things take minutes rather than milliseconds - a harvest downloads
about 28 MB of lists, a full crawl reads two thousand boards - so both run
on a worker thread, one at a time, and the page polls `GET /boards/run`.
The same shape as the Simplify tab's day runner, and for the same reason:
a request that holds a connection for three minutes is a request that
times out somewhere.
"""
from __future__ import annotations

import threading
import time
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from boardscan import crawl as crawler
from boardscan import known, registry, sources, store

router = APIRouter()

_lock = threading.Lock()
_run: dict = {"kind": "", "state": "idle", "started": "", "done": "", "result": None,
              "error": "", "progress": {}}


def _start(kind: str, work) -> dict:
    """One long job at a time. A second request is refused rather than
    queued: two crawls at once would read every board twice."""
    with _lock:
        if _run["state"] == "running":
            raise HTTPException(409, f"{_run['kind']} is already running")
        _run.update(kind=kind, state="running", started=time.strftime("%H:%M:%S"),
                    done="", result=None, error="", progress={})

    def body() -> None:
        try:
            result = work()
            with _lock:
                _run.update(state="done", result=result, done=time.strftime("%H:%M:%S"))
        except Exception as e:                  # noqa: BLE001 - reported on the page
            with _lock:
                _run.update(state="error", error=f"{type(e).__name__}: {e}",
                            done=time.strftime("%H:%M:%S"))

    threading.Thread(target=body, name=f"boardscan-{kind}", daemon=True).start()
    return dict(_run)


@router.get("/boards/stats")
def stats() -> dict:
    return {"registry": registry.stats(), "roles": store.counts(),
            "harvests": registry.harvests(), "run": dict(_run),
            "sources": [{"name": s.name, "repo": s.repo, "measured": s.measured}
                        for s in sources.SOURCES]}


@router.get("/boards/run")
def run_status() -> dict:
    return dict(_run)


class HarvestBody(BaseModel):
    sources: Optional[list[str]] = None


@router.post("/boards/harvest")
def harvest(body: HarvestBody | None = None) -> dict:
    only = (body.sources if body else None) or None
    return _start("harvest", lambda: sources.harvest(only))


class CrawlBody(BaseModel):
    limit: int = 0
    systems: list[str] = []
    only_unchecked: bool = False
    workers: int = 0


@router.post("/boards/crawl")
def crawl(body: CrawlBody | None = None) -> dict:
    body = body or CrawlBody()

    def progress(result: dict) -> None:
        with _lock:
            p = _run["progress"]
            p["companies"] = p.get("companies", 0) + 1
            p["new"] = p.get("new", 0) + result.get("new", 0)
            p["at_level"] = p.get("at_level", 0) + result.get("at_level", 0)
            p["last"] = result.get("company", "")

    return _start("crawl", lambda: crawler.crawl(
        systems=tuple(body.systems), limit=body.limit,
        workers=body.workers or crawler.DEFAULT_WORKERS,
        only_unchecked=body.only_unchecked, progress=progress))


@router.get("/boards/roles")
def roles(state: str = "", verdict: str = "", system: str = "", q: str = "",
          hide_seen: bool = False, hide_foreign: bool = False,
          limit: int = 200, offset: int = 0) -> dict:
    rows = store.listing(state=state, verdict=verdict, system=system, query=q,
                         hide_seen=hide_seen, hide_foreign=hide_foreign,
                         limit=min(limit, 500), offset=offset)
    return {"roles": [{**r.model_dump(exclude={"text"}), "id": r.id} for r in rows],
            "counts": store.counts()}


@router.get("/boards/role/{role_id}")
def role(role_id: str) -> dict:
    row = store.get(role_id)
    if row is None:
        raise HTTPException(404, "no such role")
    return {**row.model_dump(), "id": row.id}


class DecideBody(BaseModel):
    state: str
    reason: str = ""


@router.post("/boards/role/{role_id}/decide")
def decide(role_id: str, body: DecideBody) -> dict:
    if store.get(role_id) is None:
        raise HTTPException(404, "no such role")
    try:
        row = store.decide(role_id, body.state, body.reason)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {**row.model_dump(), "id": row.id}


@router.post("/boards/role/{role_id}/recheck")
def recheck(role_id: str) -> dict:
    """Ask the identity check again for one role. The crawl asks once, when
    the role is new; a job added to autopilot afterwards makes that answer
    stale, and this is the cheap way to refresh it from the page."""
    row = store.get(role_id)
    if row is None:
        raise HTTPException(404, "no such role")
    answer = known.check(row.url, title=row.title, company=row.company, text=row.text)
    store.mark_seen(role_id, answer.level, answer.job_id)
    return {"level": answer.level, "job_id": answer.job_id, "status": answer.status,
            "rule": answer.rule, "block": answer.block}


@router.post("/boards/reflag")
def reflag() -> dict:
    """Re-run the place rule over the roles already on file, after the city
    list has learned something."""
    return {"changed": crawler.reflag_places(), "counts": store.counts()}


@router.get("/boards/declines")
def declines(limit: int = 500) -> dict:
    """The week-one calibration pile, newest first."""
    return {"declines": store.declines(limit=limit)}


@router.get("/boards/companies")
def companies(system: str = "", include_dead: bool = False, limit: int = 3000) -> dict:
    rows = registry.companies(system=system, include_dead=include_dead)
    return {"companies": [{**c.model_dump(), "id": c.id, "url": c.url} for c in rows[:limit]],
            "stats": registry.stats()}


class CompanyBody(BaseModel):
    url: str = ""
    system: str = ""
    slug: str = ""
    name: str = ""


@router.post("/boards/company")
def add_company(body: CompanyBody) -> dict:
    """One company by hand: a board URL pasted in, or a system and slug.

    `scout.detect` reads the slug out of any shape of Greenhouse, Lever or
    Ashby URL, so pasting the page you would open by hand is the whole
    input.
    """
    system, slug, name = body.system, body.slug, body.name
    if body.url:
        from scout import DetectError, detect                # noqa: PLC0415
        try:
            system, args, guess = detect(body.url)
        except DetectError as e:
            raise HTTPException(400, str(e)) from e
        slug, name = args.get("slug", ""), name or guess
    try:
        company, fresh = registry.add(system, slug, name, source="by hand")
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {**company.model_dump(), "id": company.id, "url": company.url, "new": fresh}
