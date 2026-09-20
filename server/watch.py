"""The server's own eyes on a filled form: is it still there, or has it
become a confirmation page?

The capture script in the tab reports both (`/form-state` pings while
the human works, `/submitted-seen` when a confirmation shows), and it
half-works: the script is evaluated into the tab by the injector, and a
tab the injector never marked, a navigation it missed, or a page that
blocked `sessionStorage` all leave the job sitting at `filled` after
the person pressed submit. This thread does not depend on the page at
all. Every few seconds, for every job that is filled or filling and
whose fill recorded a tab, it attaches over CDP, reads the fields and
the visible text, and:

- fields still there, tab still on the job's host: keeps the look as
  `form_state.json`, the same file the ping writes, so the correction
  loop has a baseline even when no ping ever arrived;
- no form and a confirmation phrase on the page: marks the job
  submitted (`review.mark_seen`), the way the page's report would.

Nothing is pressed, typed or closed; a tab that is gone is skipped and
looked for again next round, never assumed submitted. `AUTOPILOT_WATCH=0`
turns it off.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Optional

from browser import forms
from server import corrections, queue
from server.models import Status

log = logging.getLogger("autopilot.watch")

EVERY = 4.0     # seconds between rounds
ENV = "AUTOPILOT_WATCH"

# The same phrases the page's script looks for (capture/content.js
# CONFIRMED); a match only counts once the form itself is gone, since a
# posting's own text says "thank you for your interest" often enough.
CONFIRMED = re.compile(
    r"thank(s| you) for (applying|your (application|interest|submission))"
    r"|application (has been |was )?(submitted|received|sent|complete)"
    r"|we('ve| have) received your application"
    r"|your application (is|has been) (in|complete)"
    r"|successfully (submitted|applied)", re.I)

_thread: Optional[threading.Thread] = None
_stop = threading.Event()


def looks_submitted(look: dict) -> Optional[str]:
    """The quote around the confirmation phrase, when the page has one
    and no form to speak of; None otherwise."""
    fields = look.get("fields") or {}
    if len(fields) >= corrections.MIN_FIELDS:
        return None
    text = " ".join(str(look.get("text") or "").split())
    match = CONFIRMED.search(text)
    if not match:
        return None
    start = max(0, match.start() - 40)
    return text[start:match.end() + 60].strip()


def look_at(job, app_dir: Path) -> dict:
    """One look at one job's tab. Returns what was done, for the log and
    the tests."""
    saved = corrections._read(app_dir / corrections.FILL_REPORT)
    url = corrections.cdp_url()
    target = forms.find_target(url, job.url, str(saved.get("target_id") or ""))
    if not target:
        return {"looked": False, "reason": "tab not open"}
    try:
        look = asyncio.run(forms.snapshot(url, target, detail=True))
    except Exception as error:  # noqa: BLE001 - a tab mid-navigation; next round
        return {"looked": False, "reason": str(error)[:200]}
    fields = look.get("fields") or {}
    if len(fields) >= corrections.MIN_FIELDS:
        if not forms.same_site(url, target, job.url):
            return {"looked": True, "reason": "another site's form"}
        try:
            (app_dir / corrections.FORM_STATE).write_text(json.dumps(
                {"fields": fields, "meta": look.get("meta") or {}, "target_id": target}, indent=1))
        except OSError as error:
            return {"looked": True, "reason": str(error)}
        return {"looked": True, "captured": len(fields)}
    quote = looks_submitted(look)
    if not quote:
        return {"looked": True, "reason": "no form, no confirmation"}
    from server import review
    result = review.mark_seen(job, app_dir, str(look.get("url") or ""), quote, "by the server's watch")
    return {"looked": True, "submitted": result.get("marked", False), "quote": quote}


def round_once() -> list[tuple[str, dict]]:
    out = []
    for job in queue.all_jobs():
        if job.status not in (Status.FILLED, Status.FILLING) or not job.app_dir:
            continue
        app_dir = Path(job.app_dir)
        if not (app_dir / corrections.FILL_REPORT).exists():
            continue
        try:
            result = look_at(job, app_dir)
        except Exception as error:  # noqa: BLE001 - one job never stops the round
            result = {"looked": False, "reason": str(error)[:200]}
        if result.get("submitted"):
            log.info("%s: submitted, confirmation on the page: %s", job.id, result.get("quote"))
        out.append((job.id, result))
    return out


def _loop() -> None:
    while not _stop.is_set():
        started = time.monotonic()
        try:
            round_once()
        except Exception:  # noqa: BLE001 - the watch outlives any one failure
            log.exception("watch round failed")
        _stop.wait(max(0.5, EVERY - (time.monotonic() - started)))


def start() -> bool:
    """Start the thread once. Off when `AUTOPILOT_WATCH=0`."""
    global _thread
    if os.environ.get(ENV, "1") == "0" or (_thread and _thread.is_alive()):
        return False
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="autopilot-watch", daemon=True)
    _thread.start()
    return True


def stop() -> None:
    _stop.set()
