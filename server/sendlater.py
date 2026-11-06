"""The clock behind a scheduled follow-up email.

A tick a minute, the shape of `scout/run.py` and `boardscan/run.py`: read the
outreach state, and for every email the person approved whose hour has come,
send **one** and stop until the next tick. One a minute is not pacing for its
own sake - thirteen emails handed to Gmail in the same second, from one
account, is what a provider reads as a blast, and the person asked for their
follow-ups to land at a civil time rather than all at once.

Where the lines are:

- It sends nothing it was not told to send. Only a human click can put an
  email on this queue (`server/outreach.py`'s send route), and this re-checks
  every gate at the hour rather than trusting the one at the click: a draft
  taken back off the queue, a tick withdrawn, a folder gone, all leave the
  email where it is.
- The delivery itself is not here. `server.outreach.deliver_scheduled` is,
  which keeps `mailing.send_message` in the one human-gated module invariant
  7 names, and keeps this file a clock.
- A missed morning is not sent late. `schedule.verdict` rolls anything more
  than `LATE_HOURS` past its slot to the next morning, so a laptop that was
  asleep until the afternoon does not fire a nine o'clock email at four.
- `AUTOPILOT_OUTREACH_SEND=0` stops the thread, and `TestClient(app)` built
  without `with` never starts it, which is what keeps the suite offline.
"""

from __future__ import annotations

import logging
import os
import threading
import time

from outreach import schedule, store

log = logging.getLogger("autopilot.outreach")

TICK = float(os.environ.get("AUTOPILOT_OUTREACH_TICK", "60"))
_thread: threading.Thread | None = None


def tick() -> list[str]:
    """One round. Returns the draft ids this round sent, which is 0 or 1."""
    sent: list[str] = []
    rows = store.summaries()
    for job_id in sorted(rows):
        if rows[job_id].get("scheduled", 0) < 1:
            continue
        outreach = store.get(job_id)
        for draft in outreach.drafts:
            answer = schedule.verdict(draft)
            if answer == "roll":
                store.put_draft(job_id, schedule.schedule(draft))
                log.info("outreach %s rolled to the next morning", draft.id)
                continue
            if answer != "send":
                continue
            from server import outreach as route

            try:
                route.deliver_scheduled(job_id, draft.id)
                sent.append(draft.id)
                log.info("outreach %s sent on schedule for job %s", draft.id, job_id)
            except route.MailLater as exc:
                log.info("outreach %s not sent: %s", draft.id, exc)
                store.put_draft(job_id, schedule.unschedule(draft).model_copy(
                    update={"error": f"not sent at its hour: {exc}"}))
            except Exception as exc:              # noqa: BLE001 - the clock lives on
                log.warning("outreach %s failed on schedule: %s", draft.id, exc)
            return sent                           # one an hour's worth at a time
    return sent


def _loop() -> None:
    while True:
        try:
            tick()
        except Exception as exc:                  # noqa: BLE001 - never die
            log.warning("the outreach clock stumbled: %s", exc)
        time.sleep(TICK)


def start() -> bool:
    global _thread
    if not schedule.enabled():
        return False
    if _thread is not None and _thread.is_alive():
        return False
    _thread = threading.Thread(target=_loop, name="autopilot-outreach-clock",
                               daemon=True)
    _thread.start()
    return True
