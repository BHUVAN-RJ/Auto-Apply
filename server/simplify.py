"""The Simplify new-grad list, tracked on GitHub's own page.

`github.com/SimplifyJobs/New-Grad-Positions` is a README of HTML tables:
company, role, location, an Apply link to the employer's own form, and an
age in days. The tracker is drawn **on that page** rather than in this app
(`capture/tracker.js`): a badge in a column of its own per row - APPLIED,
FILLED, IN AUTOPILOT, REJECTED, FAILED - and a bar that takes one day of
postings at a time.

Nothing is read from GitHub by the server. The page is already open in
front of the person and carries everything a capture needs, so it sends the
rows here; this module screens them, queues the ones worth applying to, and
answers what it knows about the rest. Every row carries the posting's own
UUID (the `simplify.jobs/p/<uuid>` link beside Apply), so a row is matched
to our own queue exactly rather than by guesswork.

The tracker's "Applied" column is **derived**, never written: it is the
status of that posting's job in the queue, so the four existing submission
paths (the button, the page's own confirmation ping, `server/watch.py`,
`/screen`) are still the only things that decide a job was sent. Queueing
is the same three steps as `/capture`, which means the pipeline tailors and
`settings.hold_fills` holds every one of them at APPROVED: a day's worth of
postings ends as filled forms waiting for the person's click, never as
applications. Nothing here presses Submit.
"""
from __future__ import annotations

import json
import os
import re
import threading
from collections import defaultdict
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import paths
from scout import DEFAULT_NEGATIVE, DEFAULT_POSITIVE
from scout import filter as level
from server import queue, runner, seen, settings
from server.models import Job, Status, utcnow

router = APIRouter(prefix="/simplify")

STORE_ENV = "AUTOPILOT_SIMPLIFY_FILE"

# The list's own page, and the only one the tracker is drawn on. A fork or
# a mirror of the same README is the same page as far as this is concerned;
# the path is what names it.
PAGE = re.compile(r"^https://github\.com/[^/]+/New-Grad-Positions(/|$|\?|#)", re.I)

# What a day's run covers. The README is five tables and only two of them
# are this resume's: Software Engineering and Data Science, AI & Machine
# Learning. Product, Quant and Hardware rows still get a badge and an Add
# button of their own - a decision about one job is the person's - but a
# day's run leaves them alone rather than tailoring a software resume
# against a mechanical engineering posting.
WORKED = re.compile(r"software engineering|data science|machine learning|\bai\b|\bml\b", re.I)

# A verdict that lets a job into autopilot by itself, as on any watch.
QUEUE_VERDICTS = {"ok", "caution"}

# The legend's own marks, in the Role cell. Two of them are hard and cheap:
# the list is saying the employer will not sponsor, or wants a citizen.
# Read before any page is fetched, for the same reason `filter.prescreen`
# exists - a certain no should not cost a request.
MARKS = {
    "🛂": "the list says this employer does not sponsor",
    "🇺🇸": "the list says US citizenship is required",
}

# Never queued, whatever a row says: the list's own pages are not the job.
BANNED_HOSTS = ("github.com", "githubusercontent.com", "simplify.jobs")


class Row(BaseModel):
    """One row of the table, as the page read it."""

    uuid: str
    company: str = ""
    title: str = ""
    url: str = ""
    location: str = ""
    category: str = ""
    age: str = ""              # the Age cell as printed: "0d", "12d"
    day: str = ""              # the date that age works out to, ISO
    marks: str = ""            # the legend emoji in the Role cell


class Tracked(Row):
    """A row this app has done something about."""

    # What became of the row, in one word, because the day's counters and
    # the page's badge both ask and neither should have to infer it from the
    # other fields: queued | known | skipped | reject | failed.
    outcome: str = ""
    verdict: str = ""          # the screen's: ok | caution | reject, "" when it could not run
    summary: str = ""
    reason: str = ""           # why it was not queued, in the person's words
    job_id: Optional[str] = None
    queued_at: Optional[str] = None
    seen_at: str = Field(default_factory=utcnow)


class DayRun(BaseModel):
    """One day of the list, worked through in the background."""

    day: str
    started_at: str = Field(default_factory=utcnow)
    finished_at: Optional[str] = None
    total: int = 0
    done: int = 0
    queued: list[str] = Field(default_factory=list)      # uuids
    rejected: int = 0
    skipped: int = 0           # not at level, outside the US, a hard mark, already here
    failed: int = 0            # the screen could not run
    error: str = ""

    @property
    def running(self) -> bool:
        return self.finished_at is None


# ------------------------------------------------------------------ store --
# `data/simplify.json`: the rows we have acted on and the days that have
# been worked. Small, written whole, atomically, under a lock - the same
# shape as the scout's store, and for the same reason: a page poll
# re-reading it is nothing.

_lock = threading.RLock()


def path() -> Path:
    return Path(os.environ.get(STORE_ENV, paths.DATA / "simplify.json"))


def _read() -> dict:
    p = path()
    if not p.exists():
        return {"rows": {}, "days": {}}
    try:
        data = json.loads(p.read_text() or "{}")
    except json.JSONDecodeError:
        return {"rows": {}, "days": {}}
    data.setdefault("rows", {})
    data.setdefault("days", {})
    return data


def _write(data: dict) -> None:
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, p)


def tracked() -> dict[str, Tracked]:
    with _lock:
        return {k: Tracked(**v) for k, v in _read()["rows"].items()}


def save(row: Tracked) -> Tracked:
    with _lock:
        data = _read()
        data["rows"][row.uuid] = row.model_dump()
        _write(data)
        return row


def days() -> dict[str, DayRun]:
    with _lock:
        return {k: DayRun(**v) for k, v in _read()["days"].items()}


def save_day(run: DayRun) -> DayRun:
    with _lock:
        data = _read()
        data["days"][run.day] = run.model_dump()
        _write(data)
        return run


# ----------------------------------------------------------------- lookup --
# What this app already knows about a posting. `seen.find` asks the same
# question for one URL and reads the whole queue and archive to do it; a
# page of 487 rows asks it 487 times, so the rows are indexed once and each
# lookup is a dictionary hit. The fuzzy half (same company, same role) is
# kept, because a posting captured months ago from Jobright has a different
# URL - but it only ever compares titles inside one company.


class Index:
    def __init__(self) -> None:
        self.by_url: dict[str, seen.Known] = {}
        self.by_ats: dict[tuple[str, str], seen.Known] = {}
        self.by_company: dict[str, list[seen.Known]] = defaultdict(list)
        for item in seen.known():
            self._keep(self.by_url, seen.canonical(item.url), item)
            ats = seen.ats_id(item.url)
            if ats:
                self._keep(self.by_ats, ats, item)
            company = seen.norm_company(item.company)
            if company:
                self.by_company[company].append(item)

    @staticmethod
    def _keep(index: dict, key, item: seen.Known) -> None:
        there = index.get(key)
        index[key] = item if there is None else seen.furthest([there, item])

    def find(self, row: Row) -> Optional[seen.Match]:
        def result(level_: str, item: seen.Known, reason: str) -> seen.Match:
            return seen.Match(level_, item.id, item.status, item.decided_at, item.title,
                              item.company, reason, item.reject)

        item = self.by_url.get(seen.canonical(row.url)) if row.url else None
        if item:
            return result("high", item, "same page")
        ats = seen.ats_id(row.url) if row.url else None
        if ats and ats in self.by_ats:
            return result("high", self.by_ats[ats], "same job at the employer")
        company = seen.norm_company(row.company)
        hits = [item for item in self.by_company.get(company, [])
                if not seen.expired(item)
                and seen.title_similarity(row.title, item.title) >= seen.TITLE_SIMILARITY]
        if hits:
            return result("confident", seen.furthest(hits), "same role at the same company")
        return None


# ------------------------------------------------------------------ a row --


def clean_title(title: str) -> str:
    """The role without the legend's marks."""
    for mark in MARKS:
        title = title.replace(mark, "")
    return " ".join(title.replace("🔥", "").replace("🎓", "").split())


def banned(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in BANNED_HOSTS) or seen.is_source_page(url)


def why_not(row: Row) -> str:
    """Why this row is not worth a page fetch, in the person's words, else "".

    Everything here is certain and free: no link to apply with, a mark in
    the list saying the employer will not sponsor or wants a citizen, a
    title that is not this level, a job that is only abroad.
    """
    if not row.url:
        return "no application link on the row"
    if banned(row.url):
        return "the link is to the list, not to an employer's form"
    for mark, reason in MARKS.items():
        if mark in row.marks or mark in row.title:
            return reason
    title = clean_title(row.title)
    if not level.at_level(title, DEFAULT_POSITIVE, DEFAULT_NEGATIVE):
        return "not at your level by its title"
    if level.outside_us(row.location):
        return "every location is outside the US"
    return ""


def _screen(row: Row) -> tuple[str, str]:
    """The verdict for one row: the code pre-screen, then the on-page screen.

    ("", reason) when neither could run - a posting taken down since the
    list was built is the common case, and it is not an error.
    """
    from server import screen as screen_api
    try:
        from tailor import fetch
        text = fetch.fetch(row.url).text
    except Exception as exc:  # noqa: BLE001 - the reason is the answer
        return "", f"could not read the posting: {str(exc)[:120]}"
    reason = level.prescreen(text)
    if reason:
        return "reject", reason
    try:
        result, _ = screen_api.screen_url(
            row.url, text, title=f"{clean_title(row.title)} @ {row.company}")
    except Exception as exc:  # noqa: BLE001
        return "", f"screen failed: {str(exc)[:120]}"
    return result.verdict, result.summary


def enqueue(row: Row) -> tuple[Optional[str], Optional[int]]:
    """Put one posting into autopilot: the same three steps as `/capture`.

    The pipeline starts and tailors; the fill still waits for the hold and
    for checkpoint 1, exactly as it does for a job added from its own page.
    Returns the job id and the pipeline's pid, which is what lets a day's
    run keep count of how many are tailoring at once.
    """
    job, created = queue.add(Job(url=row.url, title=clean_title(row.title),
                                 company=row.company, source="simplify"))
    pid = runner.start_pipeline(job.id) if job.status is Status.QUEUED else None
    return job.id, pid


# How many jobs from one day may be tailoring at the same time. A day is 20
# to 90 postings and `hold_fills` holds the *fills*, not the pipelines: left
# alone, this started a `pipeline.py` per row, which is that many fetches,
# model calls and LaTeX compiles at once on one laptop. Three keeps the
# run moving while the machine stays usable; the rest wait their turn in the
# run's own loop, not in the queue, so nothing is left QUEUED and forgotten.
MAX_PIPELINES = 3
PIPELINE_POLL = 2.0


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True
    return True


def _room(pids: list[int], limit: int = MAX_PIPELINES, timeout: float = 900.0) -> list[int]:
    """Wait until fewer than `limit` of these pipelines are still running."""
    import time
    until = time.monotonic() + timeout
    while True:
        pids = [pid for pid in pids if _alive(pid)]
        if len(pids) < limit or time.monotonic() > until:
            return pids
        time.sleep(PIPELINE_POLL)


def work(row: Row, index: Optional[Index] = None, force: bool = False,
         pids: Optional[list[int]] = None) -> Tracked:
    """Screen one row and queue it if the screen does not reject it.

    `force` is the person pressing Add on a row: their decision stands in
    for the screen, the job is queued, and the pipeline screens it anyway
    the way it screens every job. `pids` is a day run's list of the
    pipelines it has started, which is how it keeps `MAX_PIPELINES` of them
    going rather than one per row.
    """
    index = index or Index()
    out = Tracked(**row.model_dump())
    match = index.find(row)
    if match and not force:
        out.job_id = match.id
        out.outcome = "known"
        out.reason = f"already in autopilot ({match.status})"
        return save(out)
    if force:
        if not row.url or banned(row.url):
            out.outcome = "skipped"
            out.reason = "no employer link on this row"
            return save(out)
        out.job_id, pid = enqueue(row)
        out.queued_at = utcnow()
        out.outcome = "queued"
        out.summary = "Added by hand; the pipeline screens it like any job."
        save(out)
        if pids is not None and pid:
            pids.append(pid)
        return out
    reason = why_not(row)
    if reason:
        out.outcome = "skipped"
        out.reason = reason
        return save(out)
    out.verdict, out.summary = _screen(row)
    if out.verdict in QUEUE_VERDICTS:
        if pids is not None:
            pids[:] = _room(pids)
        out.job_id, pid = enqueue(row)
        out.queued_at = utcnow()
        out.outcome = "queued"
        if pids is not None and pid:
            pids.append(pid)
    elif out.verdict == "reject":
        out.outcome = "reject"
        out.reason = out.summary
    else:
        # No verdict at all: the posting is gone, or the fetch failed. Not
        # a rejection, and worth saying so differently.
        out.outcome = "failed" if not out.verdict else "skipped"
        out.reason = out.summary or "the screen could not run"
    return save(out)


# -------------------------------------------------------------- a day run --
# A day of the list is 20 to 90 postings, each one a page fetch and a
# screen, so it cannot be an HTTP request the page waits on. The run is a
# thread; the page asks how it is going and flips each badge as the answer
# arrives.

_worker: Optional[threading.Thread] = None


def running_day() -> Optional[str]:
    global _worker
    if _worker is not None and _worker.is_alive():
        return getattr(_worker, "day", None)
    _worker = None
    return None


def _run_day(day: str, rows: list[Row]) -> None:
    run = DayRun(day=day, total=len(rows))
    save_day(run)
    index = Index()
    pids: list[int] = []
    for row in rows:
        try:
            done = work(row, index, pids=pids)
        except Exception as exc:  # noqa: BLE001 - one bad row never ends the day
            run.failed += 1
            run.error = f"{row.company} {clean_title(row.title)}: {str(exc)[:120]}"
        else:
            if done.outcome == "queued":
                run.queued.append(row.uuid)
            elif done.outcome == "reject":
                run.rejected += 1
            elif done.outcome == "failed":
                run.failed += 1
            else:
                run.skipped += 1
        run.done += 1
        save_day(run)
    run.finished_at = utcnow()
    save_day(run)


def start_day(day: str, rows: list[Row]) -> dict:
    """Start one day's run, unless one is already going."""
    global _worker
    busy = running_day()
    if busy:
        return {"started": False, "reason": f"still working on {busy}", "day": busy}
    worked = [r for r in rows if not r.category or WORKED.search(r.category)]
    if not worked:
        return {"started": False, "reason": "no rows in the sections a day's run covers", "day": day}
    thread = threading.Thread(target=_run_day, args=(day, worked),
                              name=f"autopilot-simplify-{day}", daemon=True)
    thread.day = day  # type: ignore[attr-defined]
    _worker = thread
    thread.start()
    return {"started": True, "day": day, "total": len(worked)}


# ------------------------------------------------------------------- API --


class StatusRequest(BaseModel):
    rows: list[Row] = Field(default_factory=list)


class RowRequest(Row):
    force: bool = True          # pressing Add on a row is the decision


class DayRequest(BaseModel):
    day: str
    rows: list[Row] = Field(default_factory=list)


class DayStatusRequest(BaseModel):
    day: str = ""


def _row_state(row: Row, match: Optional[seen.Match], mine: Optional[Tracked]) -> dict:
    """What the page paints on one row. `seen` is the queue's own answer and
    the reason the tracker cannot disagree with it; `tracked` is only what
    this module did and why."""
    return {
        "uuid": row.uuid,
        "seen": match.to_dict() if match else None,
        "tracked": mine.model_dump(exclude={"marks"}) if mine else None,
    }


@router.post("/status")
def status(req: StatusRequest) -> dict:
    """What this app knows about the rows the page is showing. Reads only."""
    index = Index()
    mine = tracked()
    rows = [_row_state(row, index.find(row), mine.get(row.uuid)) for row in req.rows]
    busy = running_day()
    return {
        "rows": rows,
        "days": {d: run.model_dump() | {"running": run.running} for d, run in days().items()},
        "running": busy,
        "hold": settings.hold_fills(),
        "worked": [r.uuid for r in req.rows if not r.category or WORKED.search(r.category)],
    }


@router.post("/row")
def one_row(req: RowRequest) -> dict:
    """Add one row by hand, or screen it. The person pressed Add, so by
    default their decision stands and the row is queued."""
    row = Row(**req.model_dump(exclude={"force"}))
    if not row.uuid:
        raise HTTPException(422, "the row has no posting id")
    done = work(row, force=req.force)
    index = Index()
    return _row_state(row, index.find(row), done) | {"queued": bool(done.queued_at),
                                                    "reason": done.reason}


@router.post("/day")
def day(req: DayRequest) -> dict:
    """Work through one day of the list: screen each row, queue what the
    screen does not reject. Returns at once; the page polls `/day/status`."""
    if not req.day:
        raise HTTPException(422, "which day?")
    return start_day(req.day, req.rows)


@router.post("/day/status")
def day_status(req: DayStatusRequest) -> dict:
    """How a day's run is going, and what each of its rows came to."""
    saved = days()
    run = saved.get(req.day) if req.day else None
    mine = tracked()
    rows = {uuid: row.model_dump(exclude={"marks"}) for uuid, row in mine.items()
            if not req.day or row.day == req.day}
    return {"day": req.day, "run": (run.model_dump() | {"running": run.running}) if run else None,
            "running": running_day(), "rows": rows}


@router.get("")
def summary() -> dict:
    """The tracker as a whole, for a look from outside the page."""
    mine = tracked()
    queued = [r for r in mine.values() if r.job_id and r.queued_at]
    statuses: dict[str, str] = {}
    by_id = {job.id: job for job in queue.all_jobs()}
    for row in queued:
        job = by_id.get(row.job_id or "")
        if job:
            statuses[row.uuid] = job.status.value
    return {
        "rows": len(mine),
        "queued": len(queued),
        "applied": sum(1 for s in statuses.values() if s == Status.SUBMITTED.value),
        "days": {d: run.model_dump() | {"running": run.running} for d, run in days().items()},
        "running": running_day(),
    }
