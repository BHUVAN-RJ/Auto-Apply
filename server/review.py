"""Checkpoint 1: the review API.

Serves each application's artifacts to the review page and records the human
decision. Approval is the only path forward in the pipeline — the browser fill
loop refuses to touch a job that is not APPROVED — so this module is the gate,
not a formality.

Revision is a second tailoring pass with the reviewer's instruction appended to
the prompt. The result lands in a fresh application folder, because nothing in
an existing one is ever overwritten.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel

from archive import store
from browser import chrome
from server import queue, runner
from server.models import REJECT_LABELS, Job, RejectReason, Status

router = APIRouter(prefix="/review", tags=["review"])

# Where apply.py writes its per-job logs.
DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# Artifacts the review page may request. An allow-list rather than a path join,
# so a crafted name cannot walk out of the application folder.
ARTIFACTS = {
    "posting.md": "text/markdown",
    "resume.tex": "text/plain",
    "resume.diff": "text/plain",
    "suggestions.md": "text/markdown",
    "mismatch.md": "text/markdown",
    "screen.json": "application/json",
    "rejection.md": "text/markdown",
    "fill_notes.md": "text/markdown",
    "answers.md": "text/markdown",
    "error.txt": "text/plain",
    "resume.pdf": "application/pdf",
    "cover_letter.md": "text/markdown",
    "cover_letter.tex": "text/plain",
    "cover_letter.pdf": "application/pdf",
    "cover_letter_error.txt": "text/plain",
    "fill_screenshot.png": "image/png",
}


class Decision(BaseModel):
    note: Optional[str] = None


class FillRequest(BaseModel):
    """`force` is the second gate: the page asks the human before sending it."""

    note: Optional[str] = None
    force: bool = False


class Rejection(BaseModel):
    """A rejection always carries a reason. The note is for the specifics."""

    reason: RejectReason
    note: Optional[str] = None


class Revision(BaseModel):
    instruction: str


def _job_and_dir(job_id: str) -> tuple[Job, Path]:
    job = queue.get(job_id)
    if job is None:
        raise HTTPException(404, "unknown job")
    if not job.app_dir:
        raise HTTPException(409, "this job has not been through the pipeline yet")
    app_dir = Path(job.app_dir)
    if not app_dir.exists():
        raise HTTPException(410, f"application folder {app_dir.name} is gone")
    return job, app_dir


@router.get("/{job_id}")
def detail(job_id: str) -> dict:
    """Everything the review page needs for one application."""
    job, app_dir = _job_and_dir(job_id)
    present = [name for name in ARTIFACTS if (app_dir / name).exists()]

    def read(name: str) -> str:
        path = app_dir / name
        return path.read_text(errors="replace") if path.exists() else ""

    return {
        "id": job.id,
        "url": job.url,
        "title": job.title,
        "company": job.company,
        "source": job.source,
        "status": job.status.value,
        "folder": app_dir.name,
        "artifacts": present,
        "diff": read("resume.diff"),
        "suggestions": read("suggestions.md"),
        "mismatch": read("mismatch.md"),
        "screen": _screen(app_dir),
        "screen_error": read("screen_error.txt"),
        "cover_letter": read("cover_letter.md"),
        "cover_letter_error": read("cover_letter_error.txt"),
        "reject_reason": job.reject_reason.value if job.reject_reason else None,
        "reject_label": REJECT_LABELS.get(job.reject_reason) if job.reject_reason else None,
        "reject_note": job.reject_note,
        # Every fill appends its notes. The page shows the latest; the file
        # keeps the rest.
        "fill_notes": latest_section(read("fill_notes.md")),
        "answers": latest_section(read("answers.md")),
        "error": read("error.txt"),
        "error_detail": job.error,
        "log": _log_path(job.id),
        "history": _history(app_dir),
        "fill": _fill_state(job.id),
    }


def _fill_state(job_id: str) -> Optional[dict]:
    """The live fill for this job, so the page can show that the agent is
    working and gate a restart behind a confirmation."""
    pid = runner.fill_pid(job_id)
    if pid is None:
        return None
    started = runner.fill_started_at(job_id)
    return {
        "pid": pid,
        "started_at": started,
        "age": round(time.time() - started) if started else None,
    }


def _screen(app_dir: Path) -> Optional[dict]:
    path = app_dir / "screen.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None


def latest_section(text: str) -> str:
    """The last `---`-separated block of an appended-to artifact."""
    return text.strip().split("\n---\n")[-1].strip() if text.strip() else ""


def _log_path(job_id: str) -> Optional[str]:
    """The apply.py log for this job, if a fill has been attempted."""
    candidate = DATA_DIR / f"apply_{job_id}.log"
    return str(candidate) if candidate.exists() else None


@router.get("/{job_id}/log")
def log(job_id: str, lines: int = 120):
    """The tail of the fill log, so a failure can be read from the page."""
    path = _log_path(job_id)
    if path is None:
        raise HTTPException(404, "no fill has been attempted for this job")
    text = Path(path).read_text(errors="replace").splitlines()
    return PlainTextResponse("\n".join(text[-lines:]))


def _history(app_dir: Path) -> list[dict]:
    import json

    path = app_dir / "status.json"
    if not path.exists():
        return []
    return json.loads(path.read_text()).get("history", [])


@router.get("/{job_id}/file/{name}")
def artifact(job_id: str, name: str):
    """Serve one artifact. Only names in ARTIFACTS are reachable."""
    if name not in ARTIFACTS:
        raise HTTPException(404, "unknown artifact")
    _, app_dir = _job_and_dir(job_id)
    path = app_dir / name
    if not path.exists():
        raise HTTPException(404, f"{name} not in this application")
    if ARTIFACTS[name].startswith("text/"):
        return PlainTextResponse(path.read_text(errors="replace"))
    return FileResponse(path, media_type=ARTIFACTS[name])


@router.post("/{job_id}/approve")
def approve(job_id: str, decision: Decision) -> dict:
    """Checkpoint 1 passed, and the fill starts straight away.

    Approval is the consent, so making the human then run a command adds a
    step without adding a decision. The fill runs detached and still halts at
    checkpoint 2; nothing about the never-submit guarantee changes.
    """
    job, app_dir = _job_and_dir(job_id)
    if job.status != Status.AWAITING_REVIEW:
        raise HTTPException(409, f"job is {job.status.value}, not awaiting review")
    store.set_status(app_dir, Status.APPROVED, decision.note)
    queue.update(job_id, status=Status.APPROVED)

    pid = runner.start_fill(job_id)
    if pid:
        store.set_status(app_dir, Status.APPROVED, f"filling started (pid {pid})")
    return {"id": job_id, "status": Status.APPROVED.value, "filling": bool(pid),
            "pid": pid}


# Every state a fill may be started from. FILLING is included deliberately: a
# run whose browser died leaves the job sitting there, and the reviewer needs
# to be able to start another without first repairing the state by hand.
REFILLABLE = (Status.APPROVED, Status.FILLING, Status.FILLED, Status.FAILED)


@router.post("/{job_id}/fill")
def fill_now(job_id: str, request: FillRequest) -> dict:
    """Start or restart the fill. Repeatable, but never concurrent.

    One fill per job at a time: a second apply.py on the same Chrome tab
    fights the first. If one is running, the request is refused with the pid
    unless `force` is set, in which case the running fill is killed first.
    The page only sends `force` after the human has confirmed.
    """
    job, app_dir = _job_and_dir(job_id)
    if job.status not in REFILLABLE:
        raise HTTPException(
            409, f"job is {job.status.value}; approve it before filling"
        )
    running = runner.fill_pid(job_id)
    if running and not request.force:
        raise HTTPException(
            409, {"error": "fill_running", "pid": running,
                  "message": f"a fill is already running for this job (pid {running})"},
        )
    if running:
        runner.stop_fill(job_id)
        store.set_status(app_dir, Status.APPROVED, f"fill {running} killed for a restart")
    pid = runner.launch("apply.py", job_id)
    if not pid:
        raise HTTPException(500, "could not start apply.py")
    # Back to APPROVED so apply.py, which only touches approved jobs, will
    # pick it up, and so a stuck FILLING cannot wedge the job permanently.
    store.set_status(app_dir, Status.APPROVED, f"fill restarted (pid {pid})")
    queue.update(job_id, status=Status.APPROVED)
    return {"id": job_id, "filling": True, "pid": pid}


@router.post("/{job_id}/reject")
def reject(job_id: str, rejection: Rejection) -> dict:
    """Drop the application, recording why.

    A reason is required: a rejected job stays visible in the list, and a
    rejection with no reason tells you nothing three weeks later. The folder
    is kept intact as the record of what was tried.
    """
    _, app_dir = _job_and_dir(job_id)
    label = REJECT_LABELS[rejection.reason]
    detail = f"{label}: {rejection.note}" if rejection.note else label

    store.set_status(app_dir, Status.SKIPPED, detail)
    store.write_or_append(app_dir, "rejection.md", f"# Rejected\n\n**{label}**\n\n"
                          f"{rejection.note or '_no further detail_'}\n")
    queue.update(
        job_id,
        status=Status.SKIPPED,
        reject_reason=rejection.reason,
        reject_note=rejection.note,
    )
    return {"id": job_id, "status": Status.SKIPPED.value, "reason": label}


@router.get("/meta/reject-reasons")
def reject_reasons() -> list[dict]:
    """The reason list the review page offers."""
    return [{"value": reason.value, "label": label} for reason, label in REJECT_LABELS.items()]


@router.post("/{job_id}/submitted")
def mark_submitted(job_id: str, decision: Decision) -> dict:
    """Checkpoint 2. Only a human reaches this, after submitting by hand.

    The agent has no path to SUBMITTED: its terminal state is FILLED, and this
    endpoint is reachable only from the review page.
    """
    job, app_dir = _job_and_dir(job_id)
    # FILLING is accepted too: the human may finish and submit the form while
    # the agent is still poking at it. Their submission wins; the agent stops.
    if job.status not in (Status.FILLED, Status.FILLING):
        raise HTTPException(409, f"job is {job.status.value}, not filled")
    killed = runner.stop_fill(job_id)
    note = decision.note or "submitted by hand"
    if killed:
        note += f" (fill {killed} still running, killed)"
    store.set_status(app_dir, Status.SUBMITTED, note)
    queue.update(job_id, status=Status.SUBMITTED)

    in_progress = _in_progress(job_id)
    browser = "kept" if in_progress else ("closed" if chrome.close() else "not_running")
    return {"id": job_id, "status": Status.SUBMITTED.value,
            "browser": browser, "in_progress": in_progress}


# Statuses whose job still has a form, open or about to open, in the browser.
IN_BROWSER = (Status.APPROVED, Status.FILLING, Status.FILLED)


def _in_progress(except_id: str) -> list[dict]:
    """Other jobs that still need the browser window.

    The window is shared: closing it after one submission would take another
    job's half-filled form with it. Any other job that is approved, filling,
    or filled and waiting for its own check keeps the window open.
    """
    return [
        {"id": job.id, "title": job.title, "status": job.status.value,
         "running": runner.fill_pid(job.id) is not None}
        for job in queue.all_jobs()
        if job.id != except_id and job.status in IN_BROWSER
    ]


@router.post("/{job_id}/revise")
def revise(job_id: str, revision: Revision) -> dict:
    """Re-tailor with an extra instruction, into a new application folder.

    Imported here rather than at module scope: pipeline imports the server
    package, so importing it at the top would be circular.
    """
    from pipeline import retailor

    job, _ = _job_and_dir(job_id)
    if not revision.instruction.strip():
        raise HTTPException(400, "instruction is empty")
    app_dir = retailor(job, revision.instruction)
    return {
        "id": job_id,
        "folder": app_dir.name,
        "status": (queue.get(job_id) or job).status.value,
    }
