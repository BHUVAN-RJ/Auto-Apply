"""The bridge to the person's own Gmail tool, so a follow-up comes from them.

Autopilot's SMTP path logs into one mailbox and Gmail stamps that address on
every message leaving it; the address the person actually wants to write from
is their university one, and Gmail rewrites any `From` that is not a verified
alias of the account logged in. That was proved live on 2026-11-03, and no
app password exists for the university account - Google's own page does not
offer one on it.

The way through was already on their disk. `~/outreach/gmail-mcp` is a tool
they built: a dedicated Chrome signed into the university account by hand,
driving Gmail's own data requests to create drafts with attachments. It holds
no send call at all, by their decision, and this module does not add one.
So an approved follow-up here becomes a **draft in their own Gmail**, written
from the right address with the tailored resume on it, and the person presses
Send - which is the same gate autopilot has always had, now in the place where
the mail actually lives.

What this module is allowed to do is therefore exactly one thing: create a
draft. It shells out to `draft_batch.mjs`, which is the tool's own entry
point, so nothing here reimplements Gmail and a change in Gmail is fixed in
one place.

`AUTOPILOT_GMAIL_THING` points at another checkout; `AUTOPILOT_OUTREACH_GMAIL=0`
falls back to SMTP, which still works for anyone whose login is the address
they write from.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Optional

ENV = "AUTOPILOT_OUTREACH_GMAIL"
ROOT_ENV = "AUTOPILOT_GMAIL_THING"
DEFAULT_ROOT = Path.home() / "outreach" / "gmail-mcp"
# Their tool takes attachments by bare file name out of one folder, so that
# the text of an email can never talk an agent into attaching something else.
# The resume is copied in under its upload name before the draft is made.
ATTACHMENTS = Path.home() / ".gmail-mcp" / "attachments"
# One Chrome, one Gmail tab: two drafts at once would share it.
_LOCK = threading.Lock()
TIMEOUT = float(os.environ.get("AUTOPILOT_GMAIL_TIMEOUT", "180"))


class GmailThingError(RuntimeError):
    pass


def root() -> Path:
    return Path(os.environ.get(ROOT_ENV) or DEFAULT_ROOT).expanduser()


def script() -> Path:
    return root() / "draft_batch.mjs"


def available() -> bool:
    return script().exists() and shutil.which("node") is not None


def enabled() -> bool:
    if os.environ.get(ENV, "1").strip() in ("0", "false", "no"):
        return False
    return available()


def _run(args: list[str]) -> dict:
    try:
        done = subprocess.run(
            ["node", str(script()), *args],
            capture_output=True, text=True, timeout=TIMEOUT, cwd=str(root()),
        )
    except subprocess.TimeoutExpired:
        raise GmailThingError("the Gmail tool did not answer; is its Chrome window open?") from None
    if done.returncode != 0:
        # Its own message is the useful one ("not logged in", a Gmail 400).
        detail = (done.stderr or done.stdout or "").strip().splitlines()
        raise GmailThingError(detail[-1] if detail else "the Gmail tool failed")
    try:
        return json.loads(done.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise GmailThingError("the Gmail tool answered with something unreadable") from None


def account() -> str:
    """The address its Chrome is signed in as. The whole point of this path."""
    return str(_run(["whoami"]).get("email") or "")


def attach(path: Path) -> str:
    """Copy one file into the tool's attachment folder; returns its bare name.

    The name is the upload name the application already uses, so the recruiter
    sees the same file the employer's form received.
    """
    ATTACHMENTS.mkdir(parents=True, exist_ok=True)
    target = ATTACHMENTS / Path(path).name
    shutil.copyfile(path, target)
    return target.name


def draft(to: str, subject: str, body: str,
          attachment: Optional[Path] = None) -> dict:
    """One draft in the person's own Gmail. Nothing is sent, here or there.

    Returns the tool's own answer: the account it was made in, the Gmail
    draft id (which the Gmail connector's `delete_draft` understands), and
    whether the attachment really landed on it - read back from Gmail rather
    than assumed, because "resume attached" on the card has to be a fact.
    """
    names = [attach(attachment)] if attachment else []
    batch = {"recipients": [{
        "email": to, "subject": subject, "body": body, "attachments": names,
    }]}
    with _LOCK:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "batch.json"
            path.write_text(json.dumps(batch))
            answer = _run(["create", str(path)])

    done = answer.get("done") or {}
    if not int(done.get("created") or 0):
        # Its two refusals are both worth repeating as they are: a draft for
        # this person already exists, or they are already in Sent Mail. Both
        # mean "you have already written to them", which is the answer.
        for row in done.get("skipped") or []:
            raise GmailThingError(
                f"Gmail already has something for {to}: {row.get('reason', 'skipped')}")
        for row in done.get("failed") or []:
            raise GmailThingError(str(row.get("error") or "the draft was refused"))
        raise GmailThingError("the Gmail tool created nothing and said why in no words")
    return {
        "account": str(answer.get("account") or ""),
        "draft_id": ledger_id(str(done.get("batchId") or ""), to),
        "attached": bool((answer.get("withAttachment") or {}).get(to)) if names else False,
    }


def ledger_id(batch_id: str, to: str) -> str:
    """The Gmail draft id their tool recorded for this address.

    `createDrafts` answers with a count, and keeps the ids in its own ledger;
    this reads the last row of that batch, which is the one just written. The
    id is what the Gmail connector's `delete_draft` takes, so it is worth
    carrying onto the card.
    """
    path = Path.home() / ".gmail-mcp" / "ledger.jsonl"
    if not batch_id or not path.exists():
        return ""
    found = ""
    for line in path.read_text().splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("batchId") == batch_id and str(row.get("to", "")).lower() == to.lower():
            found = str(row.get("draftId") or "")
    return found


def send(rows: list[dict]) -> dict:
    """Send drafts that already exist in their Gmail. The outward step.

    Added 2026-11-05 at their request ("once i approve then you can start and
    then send completely"). Their tool's own `send_batch.mjs` does it, to the
    shape its AGENTS.md had already set down for a send if one were ever
    built: only drafts that exist, a daily cap, a pause between sends, no
    retry, and a stop at the first error. Each row is `{draftId, to}`, and the
    recipient is checked against the open draft before the press, so a stale
    id can never send to somebody else.
    """
    script_path = root() / "send_batch.mjs"
    if not script_path.exists():
        raise GmailThingError("this Gmail tool has no send in it")
    with _LOCK, tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "send.json"
        path.write_text(json.dumps(rows))
        try:
            done = subprocess.run(
                ["node", str(script_path), "send", str(path)],
                capture_output=True, text=True, cwd=str(root()),
                # Their batch pauses between sends, so this covers the batch.
                timeout=float(os.environ.get("AUTOPILOT_GMAIL_SEND_TIMEOUT", "1800")),
            )
        except subprocess.TimeoutExpired:
            raise GmailThingError("the Gmail tool did not finish sending") from None
    if done.returncode != 0:
        detail = (done.stderr or done.stdout or "").strip().splitlines()
        raise GmailThingError(detail[-1] if detail else "the send failed")
    try:
        return json.loads(done.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise GmailThingError("the Gmail tool answered with something unreadable") from None
