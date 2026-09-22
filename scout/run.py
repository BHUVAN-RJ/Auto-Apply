"""The scout's rounds: which watches are due, one check each, hits
recorded, the digest mailed.

`check(watch)` is the unit: fetch, keep the titles at the level, drop
the ids the watch has seen, screen each survivor with the same
`server.screen.screen_url` the banner uses (cached by URL, so a role
seen on the page later costs nothing twice), record the hits, mark the
ids seen. The first check of a new watch records everything currently
listed as seen without a hit or a mail: the point is what appears from
now on, and the person can read today's list on the page.

`verify(watch)` is the same fetch without side effects: can the
provider list roles, and are any at the level. That is what the
verifier script and the page's "Verify" run, and what turns a watch
red: a fetch that raises, or a page that lists nothing.

The thread wakes every minute and checks what is due; a watch is due
when 24 h / checks_per_day has passed since its last check.

Two kinds of watch (2026-09-21). A `notify` watch is a company where
the person can get a referral: its hits are listed and mailed with the
link, never queued by the scout. Every other watch feeds autopilot:
`level.prescreen` throws out the hard cases in code (years, clearance,
citizenship), the model screen reads the rest, and a hit the screen
does not reject is queued at once through `queue_hit`, the same three
steps as `/capture`. A reject, or a screen that could not run, stays on
the page for the person. Nothing here opens a browser or presses
anything; the pipeline and the fill take it from the queue as usual.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from scout import DEFAULT_CHECKS_PER_DAY, Hit, Posting, Watch, mail, providers, store
from scout import filter as level

log = logging.getLogger("autopilot.scout")

ENV = "AUTOPILOT_SCOUT"
TICK = 60.0                 # seconds between looks at what is due
SCREEN_LIMIT = 25           # new roles screened per check; the rest wait for the next round
QUEUE_VERDICTS = ("ok", "caution")   # what a non-notify watch sends into autopilot; reject and "" wait for the person
LISTED_KEEP = 100           # roles at the level kept on the watch row for the page, per check
TEXT_KEEP = 6000            # characters of description kept on the hit

_thread: Optional[threading.Thread] = None
_stop = threading.Event()
_busy: set[str] = set()     # watch ids being checked right now
_busy_lock = threading.Lock()


def checks_per_day(watch: Watch) -> int:
    if watch.checks_per_day:
        return max(1, min(48, watch.checks_per_day))
    from server import settings
    return max(1, min(48, int(settings.load().get("scout_checks_per_day", DEFAULT_CHECKS_PER_DAY))))


def interval(watch: Watch) -> float:
    return 86400.0 / checks_per_day(watch)


def due(watch: Watch, now: Optional[datetime] = None) -> bool:
    if not watch.enabled:
        return False
    if not watch.last_checked:
        return True
    now = now or datetime.now(timezone.utc)
    last = datetime.fromisoformat(watch.last_checked)
    return (now - last).total_seconds() >= interval(watch)


def next_check(watch: Watch) -> Optional[str]:
    if not watch.enabled:
        return None
    if not watch.last_checked:
        return "now"
    last = datetime.fromisoformat(watch.last_checked)
    return datetime.fromtimestamp(last.timestamp() + interval(watch), tz=timezone.utc).isoformat(timespec="seconds")


def listed_row(p: Posting) -> dict:
    """What the page shows for a role at the level: no description."""
    return {"id": p.id, "title": p.title, "url": p.url, "location": p.location, "posted": p.posted}


def add_listed(watch_id: str, posting_id: str) -> dict:
    """Put one of the roles listed on the watch into autopilot by hand:
    a hit is recorded for it (unscreened; the pipeline screens) and
    queued the usual way."""
    watch = store.get_watch(watch_id)
    if watch is None:
        return {"ok": False, "reason": "no such watch"}
    row = next((r for r in watch.listed if r["id"] == posting_id), None)
    if row is None:
        return {"ok": False, "reason": "not on the list any more; check the page again"}
    posting = Posting(id=row["id"], title=row["title"], url=row["url"], location=row.get("location", ""),
                      posted=row.get("posted", ""))
    hit = Hit(watch_id=watch.id, company=watch.company, posting=posting.to_dict())
    store.add_hits([hit])
    store.update_watch(watch.id, seen_ids=sorted(set(watch.seen_ids) | {posting.id}))
    return queue_hit(hit.id)


def verify(watch: Watch) -> dict:
    """Can the page be read, and does it list roles at the level. No writes."""
    started = time.monotonic()
    try:
        rows = providers.fetch(watch)
    except Exception as exc:  # noqa: BLE001 - the reason is the result
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}", "total": 0, "matching": 0,
                "sample": [], "seconds": round(time.monotonic() - started, 1)}
    matching = level.matching(watch, rows)
    result = {"ok": bool(rows), "total": len(rows), "matching": len(matching),
              "sample": [p.title for p in matching[:8]], "seconds": round(time.monotonic() - started, 1)}
    if not rows:
        result["error"] = "the page lists no roles at all; wrong URL, or the site changed"
    return result


def _screen(posting: Posting, company: str) -> tuple[str, str]:
    """Verdict and summary: the code pre-screen first, then the on-page
    screen; ("", reason) when neither can run. A screen failure never
    loses the hit."""
    from server import screen as screen_api
    text = posting.text
    if not text.strip():
        try:
            from tailor import fetch
            text = fetch.fetch(posting.url).text
        except Exception as exc:  # noqa: BLE001
            return "", f"no description: {str(exc)[:120]}"
    reason = level.prescreen(text)
    if reason:
        return "reject", reason
    try:
        result, _ = screen_api.screen_url(posting.url, text, title=f"{posting.title} @ {company}")
    except Exception as exc:  # noqa: BLE001
        return "", f"screen failed: {str(exc)[:120]}"
    return result.verdict, result.summary


def check(watch: Watch, screen: bool = True) -> dict:
    """One check of one watch. Returns what happened; the watch row and
    the hits are written here."""
    with _busy_lock:
        if watch.id in _busy:
            return {"checked": False, "reason": "already running"}
        _busy.add(watch.id)
    try:
        return _check(watch, screen)
    finally:
        with _busy_lock:
            _busy.discard(watch.id)


def _broken(watch: Watch, error: str, **fields) -> dict:
    """Mark the watch red and say so by mail, once, until it reads again."""
    log.warning("scout %s (%s): %s", watch.company, watch.provider, error)
    saved = store.update_watch(watch.id, error=error, **fields)
    mailed = False
    if saved and not saved.broken_mailed and mail.configured():
        try:
            mail.send(*mail.broken_notice(saved))
            mailed = True
        except mail.MailError as exc:
            log.warning("scout broken mail: %s", exc)
        else:
            store.update_watch(watch.id, broken_mailed=True)
    return {"checked": True, "ok": False, "error": error, "new": 0, "broken_mailed": mailed}


def _check(watch: Watch, screen: bool) -> dict:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        rows = providers.fetch(watch)
    except Exception as exc:  # noqa: BLE001
        return _broken(watch, f"{type(exc).__name__}: {str(exc)[:200]}", last_checked=now)
    if not rows:
        return _broken(watch, "the page lists no roles at all; wrong URL, or the site changed",
                       last_checked=now, last_total=0, last_matching=0)

    matching = level.matching(watch, rows)
    seen = set(watch.seen_ids)
    first_run = watch.last_ok is None and not seen
    fresh = [p for p in matching if p.id not in seen]
    hits: list[Hit] = []
    queued: list[str] = []
    if not first_run:
        for posting in fresh[:SCREEN_LIMIT]:
            verdict, summary = _screen(posting, watch.company) if screen else ("", "")
            posting.text = posting.text[:TEXT_KEEP]
            hits.append(Hit(watch_id=watch.id, company=watch.company, posting=posting.to_dict(),
                            verdict=verdict, summary=summary))
        added = store.add_hits(hits)
        seen |= {p.id for p in fresh[:SCREEN_LIMIT]}
        if not watch.notify:
            for hit in added:
                if hit.verdict in QUEUE_VERDICTS:
                    try:
                        queue_hit(hit.id)
                        queued.append(hit.id)
                    except Exception:  # noqa: BLE001 - the hit stays on the page
                        log.exception("scout queue %s", hit.id)
    else:
        added = []
        seen |= {p.id for p in matching}
    # Ids no longer listed are dropped so the file does not grow forever;
    # a role that comes back after a gap is news again.
    listed = {p.id for p in rows}
    store.update_watch(watch.id, last_checked=now, last_ok=now, last_total=len(rows),
                       last_matching=len(matching), error=None, broken_mailed=False,
                       seen_ids=sorted(seen & listed), listed=[listed_row(p) for p in matching[:LISTED_KEEP]])
    return {"checked": True, "ok": True, "total": len(rows), "matching": len(matching),
            "new": len(added), "queued": len(queued), "first_run": first_run, "hits": [h.id for h in added]}


def mail_new() -> dict:
    """One digest for every hit not yet mailed: the ones to ask a referral
    for, with the link, and the ones already in autopilot. Rejects stay
    on the page and out of the mail."""
    if not mail.configured():
        return {"sent": False, "reason": "mail not set up"}
    watches = {w.id: w for w in store.watches()}
    pending = [h for h in store.hits() if not h.mailed and h.status in ("new", "queued")]
    rows = [(watches[h.watch_id], h) for h in pending if h.watch_id in watches and h.verdict != "reject"]
    for h in pending:
        if h.verdict == "reject" or h.watch_id not in watches:
            store.update_hit(h.id, mailed=True)
    if not rows:
        return {"sent": False, "reason": "nothing new"}
    subject, text, html = mail.digest(rows)
    try:
        mail.send(subject, text, html)
    except mail.MailError as exc:
        log.warning("scout mail: %s", exc)
        return {"sent": False, "reason": str(exc)}
    for _, h in rows:
        store.update_hit(h.id, mailed=True)
    return {"sent": True, "count": len(rows), "subject": subject}


def round_once(force: bool = False) -> list[tuple[str, dict]]:
    """Check every watch that is due (every enabled one when `force`),
    then mail whatever is new. Serial: the providers are other people's
    servers and a burst is not needed."""
    results = []
    for watch in store.watches():
        if force and watch.enabled or due(watch):
            results.append((watch.id, check(watch)))
    if any(r.get("new") for _, r in results):
        mail_new()
    return results


def queue_hit(hit_id: str) -> dict:
    """Put a hit into autopilot: the same three steps as `/capture`. The
    pipeline starts, the fill still waits at checkpoint 1 like any job."""
    from server import postings, queue, runner
    from server.models import Job
    hit = store.get_hit(hit_id)
    if hit is None:
        return {"ok": False, "reason": "no such hit"}
    p = hit.posting
    job, created = queue.add(Job(url=p["url"], title=p["title"], company=hit.company, source="scout"))
    if p.get("text"):
        postings.save_captured(job.id, postings.Saved(url=p["url"], title=p["title"], text=p["text"], company=hit.company))
    pid = runner.start_pipeline(job.id) if job.status.value == "queued" else None
    store.update_hit(hit_id, status="queued", job_id=job.id)
    return {"ok": True, "id": job.id, "created": created, "processing": bool(pid)}


def _loop() -> None:
    while not _stop.is_set():
        started = time.monotonic()
        try:
            round_once()
        except Exception:  # noqa: BLE001 - the scout outlives any one failure
            log.exception("scout round failed")
        _stop.wait(max(1.0, TICK - (time.monotonic() - started)))


def start() -> bool:
    """Start the thread once. Off when `AUTOPILOT_SCOUT=0`."""
    global _thread
    if os.environ.get(ENV, "1") == "0" or (_thread and _thread.is_alive()):
        return False
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="autopilot-scout", daemon=True)
    _thread.start()
    return True


def stop() -> None:
    _stop.set()


def running() -> bool:
    return bool(_thread and _thread.is_alive())
