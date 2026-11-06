"""The bridge to the person's own LinkedIn tool.

An application that has gone in is worth one email and, often, one connection
request: the recruiter who will read the pile, and the engineers on the team.
The person already built the tool for the second half - `~/outreach/linkedin-mcp`
drives a dedicated Chrome signed into their own account - and it carries the
two rules this module must not weaken:

- **A batch is prepared, shown, and only then sent.** Nothing here sends
  without a press on the review page, the same gate every outward action in
  autopilot has.
- **Their tracker owns the cap.** 14 invites per rolling 24 hours, one to
  three minutes apart, no retry, and a stop at the first failure. This module
  asks it how much room is left and never counts for itself, because its log
  is the one that also holds what the person sent by hand.

Autopilot's part is the writing and the queue: who at this employer is worth
asking, a note per person to `docs/connection-note-guide.md`, and the press.

`AUTOPILOT_LINKEDIN_THING` points elsewhere; `AUTOPILOT_OUTREACH_LINKEDIN=0`
turns the whole thing off.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

ENV = "AUTOPILOT_OUTREACH_LINKEDIN"
ROOT_ENV = "AUTOPILOT_LINKEDIN_THING"
DEFAULT_ROOT = Path.home() / "outreach" / "linkedin-mcp"
NOTE_LIMIT = 300
# One Chrome, one LinkedIn session: two of these at once is two tabs fighting.
_LOCK = threading.Lock()
# Sending is paced by their own batch runner, up to three minutes per invite,
# so the timeout has to cover a full batch rather than one request.
TIMEOUT = float(os.environ.get("AUTOPILOT_LINKEDIN_TIMEOUT", "2400"))


class LinkedInThingError(RuntimeError):
    pass


def root() -> Path:
    return Path(os.environ.get(ROOT_ENV) or DEFAULT_ROOT).expanduser()


def script() -> Path:
    return root() / "people.mjs"


def available() -> bool:
    return script().exists() and shutil.which("node") is not None


def enabled() -> bool:
    if os.environ.get(ENV, "1").strip() in ("0", "false", "no"):
        return False
    return available()


def guide() -> str:
    """Their own note-writing guide, which the model is given verbatim."""
    path = root() / "docs" / "connection-note-guide.md"
    try:
        return path.read_text()
    except OSError:
        return ""


def _run(args: list[str], timeout: float | None = None) -> dict:
    try:
        done = subprocess.run(
            ["node", str(script()), *args], capture_output=True, text=True,
            timeout=timeout or TIMEOUT, cwd=str(root()),
        )
    except subprocess.TimeoutExpired:
        raise LinkedInThingError("the LinkedIn tool did not answer in time") from None
    if done.returncode != 0:
        detail = (done.stderr or done.stdout or "").strip().splitlines()
        raise LinkedInThingError(detail[-1] if detail else "the LinkedIn tool failed")
    try:
        return json.loads(done.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise LinkedInThingError("the LinkedIn tool answered with something unreadable") from None


def room() -> int:
    """Invites left in their rolling 24 hours, counted by their own tracker."""
    return int(_run(["status"], timeout=120).get("capLeftToday") or 0)


def _rows_file(rows: list[dict], folder: str) -> str:
    path = Path(folder) / "people.json"
    path.write_text(json.dumps(rows))
    return str(path)


def prepare(rows: list[dict]) -> dict:
    """Check a list of `{profileUrl, note}` without sending anything.

    It resolves each profile to the member it names, drops anyone already
    invited, and says how much room the cap leaves - all of which is worth
    knowing before the person is asked to approve a list.
    """
    for row in rows:
        _check(row)
    with _LOCK, tempfile.TemporaryDirectory() as folder:
        return _run(["prepare", _rows_file(rows, folder)], timeout=600)


def invite(rows: list[dict]) -> dict:
    """Really send these connection requests. Only a human press reaches here."""
    for row in rows:
        _check(row)
    with _LOCK, tempfile.TemporaryDirectory() as folder:
        return _run(["invite", _rows_file(rows, folder)])


def _check(row: dict) -> None:
    note = str(row.get("note") or "").strip()
    if not row.get("profileUrl"):
        raise LinkedInThingError("every invite needs a LinkedIn profile URL")
    if not note:
        raise LinkedInThingError(f"{row['profileUrl']} has no note, and every invite carries one")
    if len(note) > NOTE_LIMIT:
        raise LinkedInThingError(f"the note for {row['profileUrl']} is over {NOTE_LIMIT} characters")
