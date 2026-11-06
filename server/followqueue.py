"""Every submitted application goes through the follow-up step, not ten of them.

The person's own instruction, after the first version offered a button that
prepared the newest ten: "just for the companies where we have applied and
not sent we do these search and everything thats it why 10 and all all fkn
companies should after submission go to this state of checking and then I
click done maybe and they go under submitted like earlier thats the flow."

So this is a queue with no cap and no button: a thread that takes the
submitted applications with no follow-up yet, newest first, and prepares them
one at a time until there are none left. A new submission still prepares
itself the moment it happens (`outreach.follow.start` from the submission
paths); this is for the backlog, and it goes quiet once the backlog is gone.

One at a time, deliberately. Each job is a web search for the people, up to
three address lookups and up to three model calls, so ten in parallel is ten
of each at once on one laptop - and the credits, where the free ladder misses,
are the person's money.

`settings.outreach_backfill` is the switch, on by default, because an
application nobody was written to is the thing the whole feature exists to
fix. `AUTOPILOT_OUTREACH_BACKFILL=0` stops it without touching the file.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

from outreach import follow
from outreach import store as outreach_store

log = logging.getLogger("autopilot.outreach")

ENV = "AUTOPILOT_OUTREACH_BACKFILL"
TICK = float(os.environ.get("AUTOPILOT_OUTREACH_BACKFILL_TICK", "20"))
_thread: threading.Thread | None = None
_state: dict = {"job": "", "done": 0, "left": 0, "last": ""}
_lock = threading.Lock()


def enabled() -> bool:
    if os.environ.get(ENV, "1").strip() in ("0", "false", "no"):
        return False
    from server import settings

    return settings.outreach_backfill()


def waiting() -> list[str]:
    """Submitted applications with no follow-up prepared, newest first.

    "No follow-up prepared" is an empty `prepare_status`: a job that was
    tried and found nobody is finished with, not retried for ever, and the
    person can press Prepare again on its own card.
    """
    from server import queue
    from server.models import Status

    rows = outreach_store.summaries()
    out = []
    for job in queue.all_jobs():
        if job.status is not Status.SUBMITTED:
            continue
        if not job.app_dir or not Path(job.app_dir).exists():
            continue
        if rows.get(job.id, {}).get("prepare_status", ""):
            continue
        out.append((job.added_at or "", job.id))
    out.sort(reverse=True)
    return [job_id for _, job_id in out]


def status() -> dict:
    with _lock:
        state = dict(_state)
    state["on"] = enabled()
    state["left"] = len(waiting())
    return state


def tick() -> str:
    """Prepare one job. Returns its id, or "" when there was nothing to do."""
    if not enabled():
        return ""
    queued = waiting()
    with _lock:
        _state["left"] = len(queued)
    if not queued:
        with _lock:
            _state["job"] = ""
        return ""
    job_id = queued[0]
    with _lock:
        _state["job"] = job_id
    if not follow.start(job_id):
        # Already running or already prepared by somebody else; the next tick
        # will see it gone from the list.
        return ""
    follow.wait(job_id, timeout=900)
    with _lock:
        _state["done"] += 1
        _state["job"] = ""
        _state["last"] = job_id
    log.info("follow-up prepared for %s in the backfill", job_id)
    return job_id


def _loop() -> None:
    while True:
        try:
            tick()
        except Exception as exc:                  # noqa: BLE001 - never die
            log.warning("the follow-up backfill stumbled: %s", exc)
        time.sleep(TICK)


def start() -> bool:
    global _thread
    if _thread is not None and _thread.is_alive():
        return False
    # Started even when the switch is off, because the switch is read on
    # every tick: flipping it on the page must not need a restart.
    _thread = threading.Thread(target=_loop, name="autopilot-outreach-backfill",
                               daemon=True)
    _thread.start()
    return True
