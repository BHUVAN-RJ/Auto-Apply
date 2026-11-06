"""When an approved email actually goes out.

The person's own words: "I want to schedule them for 9 am in the morning.
When I click Send, it has to be drafted and scheduled for 9 am, so I don't
want to send them at random times." A follow-up reviewed at one in the
morning should not land at one in the morning.

SMTP has no "send later", so the waiting is ours to do. This module decides
*when*; the sending itself stays where the invariant keeps it, in the one
human-gated route in `server/outreach.py` (invariant 7), and
`server/sendlater.py` is the thread that calls that route's own delivery when
an hour comes round.

Nothing here weakens the standing rule. The click is still the only thing
that can put an email on this queue, a guessed address still needs the tick
before it can be queued at all, and a scheduled email can be taken back off
(`unschedule`) at any time before it goes.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Optional

from .models import Draft

ENV = "AUTOPILOT_OUTREACH_SEND"        # 0 stops the thread; drafts still queue
HOUR_ENV = "AUTOPILOT_OUTREACH_HOUR"   # the local hour they land at
# Hours after the slot that still count as "this morning". A laptop asleep
# until the afternoon must not fire a nine o'clock email at four, which is
# exactly the random timing this exists to prevent: it rolls to the next
# morning instead.
LATE_HOURS = 4


def enabled() -> bool:
    return os.environ.get(ENV, "1").strip() not in ("0", "false", "no")


def hour() -> int:
    try:
        value = int(os.environ.get(HOUR_ENV, "9"))
    except ValueError:
        return 9
    return value if 0 <= value <= 23 else 9


def next_slot(now: Optional[datetime] = None) -> datetime:
    """The next time that hour comes round, in this machine's own zone.

    Local on purpose: they said nine in the morning, and the morning they
    mean is the one they are standing in.
    """
    now = now or datetime.now().astimezone()
    slot = now.replace(hour=hour(), minute=0, second=0, microsecond=0)
    return slot if slot > now else slot + timedelta(days=1)


def schedule(draft: Draft, now: Optional[datetime] = None) -> Draft:
    """The same draft, approved and waiting for its hour."""
    return draft.model_copy(update={
        "status": "scheduled",
        "send_at": next_slot(now).isoformat(timespec="seconds"),
        "error": "",
    })


def unschedule(draft: Draft) -> Draft:
    """Taken back off the queue and editable again: they changed their mind."""
    return draft.model_copy(update={"status": "draft", "send_at": None})


def when(draft: Draft) -> Optional[datetime]:
    if not draft.send_at:
        return None
    try:
        return datetime.fromisoformat(draft.send_at)
    except ValueError:
        return None


def verdict(draft: Draft, now: Optional[datetime] = None) -> str:
    """What to do with one scheduled draft: send, wait, roll, or nothing."""
    if draft.status != "scheduled":
        return ""
    now = now or datetime.now().astimezone()
    due = when(draft)
    if due is None:
        return "roll"                 # no readable time: give it the next one
    if now < due:
        return "wait"
    if now - due > timedelta(hours=LATE_HOURS):
        return "roll"
    return "send"
