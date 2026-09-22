"""The Scout tab's API: watches on companies' careers pages, the hits
they found, the mail that tells the person.

Thin over `scout/`. A watch is added by URL: `scout.detect` names the
provider or refuses; the new watch is verified on the spot and answers
red when the page cannot be read. Every write here is the person's:
adding, checking now, changing the frequency, queueing a hit.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from scout import DetectError, Watch, detect, mail, run, store
from server import settings

router = APIRouter(prefix="/scout")


class WatchRequest(BaseModel):
    url: str
    company: str = ""
    query: Optional[str] = None
    referrer: str = ""
    checks_per_day: Optional[int] = None


class WatchUpdate(BaseModel):
    company: Optional[str] = None
    query: Optional[str] = None
    referrer: Optional[str] = None
    checks_per_day: Optional[int] = None
    enabled: Optional[bool] = None
    positive: Optional[list[str]] = None
    negative: Optional[list[str]] = None


class FrequencyRequest(BaseModel):
    checks_per_day: int


def watch_row(w: Watch) -> dict:
    row = w.model_dump(exclude={"seen_ids"})
    row.update(id=w.id, broken=w.broken, checks_per_day_effective=run.checks_per_day(w),
               next_check=run.next_check(w), seen=len(w.seen_ids))
    return row


def hit_row(h, watches: dict[str, Watch]) -> dict:
    row = h.model_dump()
    w = watches.get(h.watch_id)
    row.update(id=h.id, referrer=w.referrer if w else "",
               note=mail.referral_note(w, h) if w else "")
    row["posting"] = {k: v for k, v in h.posting.items() if k != "text"}
    return row


@router.get("")
def get_status() -> dict:
    watches = store.watches()
    by_id = {w.id: w for w in watches}
    hits = sorted(store.hits(), key=lambda h: h.found_at, reverse=True)
    return {
        "running": run.running(),
        "broken": [w.company for w in watches if w.broken],
        "checks_per_day": int(settings.load().get("scout_checks_per_day", run.DEFAULT_CHECKS_PER_DAY)),
        "mail": mail.status(),
        "watches": [watch_row(w) for w in watches],
        "hits": [hit_row(h, by_id) for h in hits],
    }


@router.post("/watch")
def add_watch(req: WatchRequest) -> dict:
    try:
        provider, args, company = detect(req.url)
    except DetectError as exc:
        raise HTTPException(400, str(exc))
    query = args.pop("query", "")
    if req.query is not None:
        query = req.query
    watch = Watch(url=req.url.strip(), company=req.company.strip() or company, provider=provider, args=args,
                  query=query, referrer=req.referrer.strip(), checks_per_day=req.checks_per_day)
    watch, created = store.add_watch(watch)
    if not created:
        return {"created": False, "watch": watch_row(watch), "verify": None}
    # Verified now, so a URL that cannot be read is red before the page
    # is even refreshed, and the first check seeds the seen list.
    result = run.verify(watch)
    if result["ok"]:
        run.check(watch)
    else:
        run._broken(watch, result["error"])
    return {"created": True, "watch": watch_row(store.get_watch(watch.id) or watch), "verify": result}


@router.patch("/watch/{watch_id}")
def update_watch(watch_id: str, req: WatchUpdate) -> dict:
    fields = {k: v for k, v in req.model_dump().items() if v is not None}
    if "checks_per_day" in fields and not 1 <= fields["checks_per_day"] <= 48:
        raise HTTPException(400, "checks per day: 1 to 48")
    watch = store.update_watch(watch_id, **fields)
    if watch is None:
        raise HTTPException(404, "no such watch")
    return watch_row(watch)


@router.delete("/watch/{watch_id}")
def delete_watch(watch_id: str) -> dict:
    return {"removed": store.remove_watch(watch_id)}


@router.post("/watch/{watch_id}/check")
def check_watch(watch_id: str) -> dict:
    watch = store.get_watch(watch_id)
    if watch is None:
        raise HTTPException(404, "no such watch")
    result = run.check(watch)
    if result.get("new"):
        result["mail"] = run.mail_new()
    result["watch"] = watch_row(store.get_watch(watch_id) or watch)
    return result


@router.post("/watch/{watch_id}/verify")
def verify_watch(watch_id: str) -> dict:
    watch = store.get_watch(watch_id)
    if watch is None:
        raise HTTPException(404, "no such watch")
    result = run.verify(watch)
    if result["ok"]:
        store.update_watch(watch_id, error=None, broken_mailed=False)
    else:
        run._broken(watch, result["error"])
    result["watch"] = watch_row(store.get_watch(watch_id) or watch)
    return result


@router.post("/verify")
def verify_all() -> dict:
    """Every watch, whether it can list roles at the level right now."""
    out = []
    for watch in store.watches():
        result = run.verify(watch)
        if result["ok"]:
            store.update_watch(watch.id, error=None, broken_mailed=False)
        else:
            run._broken(watch, result["error"])
        out.append({"id": watch.id, "company": watch.company, "url": watch.url, **result})
    return {"results": out, "broken": sum(1 for r in out if not r["ok"])}


@router.post("/check")
def check_all() -> dict:
    results = run.round_once(force=True)
    return {"results": dict(results), "mail": run.mail_new()}


@router.post("/frequency")
def set_frequency(req: FrequencyRequest) -> dict:
    if not 1 <= req.checks_per_day <= 48:
        raise HTTPException(400, "checks per day: 1 to 48")
    settings.save(scout_checks_per_day=req.checks_per_day)
    return {"checks_per_day": req.checks_per_day}


@router.post("/hits/{hit_id}/queue")
def queue_hit(hit_id: str) -> dict:
    result = run.queue_hit(hit_id)
    if not result["ok"]:
        raise HTTPException(404, result["reason"])
    return result


@router.post("/hits/{hit_id}/dismiss")
def dismiss_hit(hit_id: str) -> dict:
    hit = store.update_hit(hit_id, status="dismissed")
    if hit is None:
        raise HTTPException(404, "no such hit")
    return {"ok": True}


@router.post("/mail/test")
def mail_test() -> dict:
    try:
        mail.send("Scout: test mail", "The scout can reach you here. Nothing found yet.\n")
    except mail.MailError as exc:
        raise HTTPException(400, str(exc))
    return {"sent": True, "to": mail.config()["to"]}
