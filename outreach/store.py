"""Atomic outreach state plus immutable records of messages actually sent."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

import paths
from archive import store as archive

from .models import Contact, Draft, Outreach, now

STATE: Optional[Path] = None
CACHE: Optional[Path] = None
CACHE_TTL = 30 * 24 * 60 * 60
_LOCK = threading.RLock()


def state_path() -> Path:
    return STATE or paths.DATA / "outreach.json"


def cache_path() -> Path:
    return CACHE or paths.DATA / "outreach_cache.json"


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text() or "{}")
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def get(job_id: str) -> Outreach:
    with _LOCK:
        raw = _read(state_path()).get(job_id)
    if not isinstance(raw, dict):
        return Outreach(job_id=job_id)
    try:
        return Outreach.model_validate(raw)
    except ValueError:
        return Outreach(job_id=job_id, error="saved outreach state could not be read")


def save(outreach: Outreach) -> Outreach:
    outreach.updated_at = now()
    with _LOCK:
        data = _read(state_path())
        data[outreach.job_id] = outreach.model_dump(mode="json")
        _write(state_path(), data)
    return outreach


def add_contacts(job_id: str, contacts: list[Contact], note: str = "") -> Outreach:
    outreach = get(job_id)
    by_id = {row.id: row for row in outreach.contacts}
    for contact in contacts:
        by_id[contact.id] = contact
    role_order = {"recruiter": 0, "hiring_manager": 1}
    outreach.contacts = sorted(
        by_id.values(), key=lambda row: (role_order.get(row.role, 9), row.name.lower()),
    )
    if note:
        outreach.lookup_note = note
    outreach.error = ""
    return save(outreach)


def put_draft(job_id: str, draft: Draft) -> Outreach:
    outreach = get(job_id)
    existing = next((index for index, row in enumerate(outreach.drafts)
                     if row.id == draft.id), None)
    draft.updated_at = now()
    if existing is None:
        outreach.drafts.append(draft)
    else:
        outreach.drafts[existing] = draft
    outreach.active_draft = draft.id
    outreach.error = ""
    return save(outreach)


def put_invite(job_id: str, invite) -> "Outreach":
    """One connection request, written or updated in place."""
    outreach = get(job_id)
    existing = next((index for index, row in enumerate(outreach.invites)
                     if row.id == invite.id), None)
    invite.updated_at = now()
    if existing is None:
        outreach.invites.append(invite)
    else:
        outreach.invites[existing] = invite
    return save(outreach)


def immutable_sent_record(app_dir: Path, contact: Contact, draft: Draft,
                          attachment: Optional[Path]) -> Path:
    payload = {
        "draft": draft.model_dump(mode="json"),
        "contact": contact.model_dump(mode="json"),
        "attachment": str(attachment.name) if attachment else None,
        "recorded_at": now(),
    }
    return archive.write(
        app_dir,
        f"outreach_{draft.id}_sent.json",
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
    )


def cache_get(provider: str, key: str) -> Optional[dict]:
    cache_key = f"{provider}:{key.strip().lower()}"
    with _LOCK:
        row = _read(cache_path()).get(cache_key)
    if not isinstance(row, dict) or time.time() - float(row.get("saved_at", 0)) > CACHE_TTL:
        return None
    value = row.get("value")
    return value if isinstance(value, dict) else None


def cache_put(provider: str, key: str, value: dict) -> None:
    cache_key = f"{provider}:{key.strip().lower()}"
    with _LOCK:
        data = _read(cache_path())
        data[cache_key] = {"saved_at": time.time(), "value": value}
        _write(cache_path(), data)


# Statuses a draft counts under. A failed send is not something to click on,
# so it is not counted as waiting; it shows on the job's own card.
LIVE_DRAFTS = ("draft", "scheduled", "sending", "sent")


def summaries() -> dict[str, dict]:
    """One line per job that has outreach state, keyed by job id.

    Read in a single pass rather than per job. A submitted application is not
    finished until its follow-up emails have gone, so the review page's list
    asks for this with every refresh, for every row at once.
    """
    with _LOCK:
        data = _read(state_path())
    out: dict[str, dict] = {}
    for job_id, raw in data.items():
        if not isinstance(raw, dict):
            continue
        drafts = [row for row in raw.get("drafts", []) if isinstance(row, dict)]
        kept = [row for row in drafts if row.get("status") in LIVE_DRAFTS]
        out[str(job_id)] = {
            "prepare_status": str(raw.get("prepare_status") or ""),
            "total": len(kept),
            "sent": len([row for row in kept if row.get("status") == "sent"]),
            "unsent": len([row for row in kept
                           if row.get("status") in ("draft", "sending")]),
            # Approved and waiting for its hour: not the person's move any
            # more, so the list says the time rather than "send".
            "scheduled": len([row for row in kept
                              if row.get("status") == "scheduled"]),
            "next_send": min((str(row.get("send_at") or "") for row in kept
                              if row.get("status") == "scheduled"), default=""),
            "contacts": len([row for row in raw.get("contacts", [])
                             if isinstance(row, dict)]),
            "done": bool(raw.get("done_at")),
            "note": str(raw.get("lookup_note") or ""),
            "error": str(raw.get("error") or ""),
        }
    return out
