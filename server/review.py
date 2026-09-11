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

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel

from archive import store
from server import queue
from server.models import REJECT_LABELS, Job, RejectReason, Status

router = APIRouter(prefix="/review", tags=["review"])

# Artifacts the review page may request. An allow-list rather than a path join,
# so a crafted name cannot walk out of the application folder.
ARTIFACTS = {
    "posting.md": "text/markdown",
    "resume.tex": "text/plain",
    "resume.diff": "text/plain",
    "suggestions.md": "text/markdown",
    "mismatch.md": "text/markdown",
    "rejection.md": "text/markdown",
    "fill_notes.md": "text/markdown",
    "error.txt": "text/plain",
    "resume.pdf": "application/pdf",
    "fill_screenshot.png": "image/png",
}


class Decision(BaseModel):
    note: Optional[str] = None


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
        "reject_reason": job.reject_reason.value if job.reject_reason else None,
        "reject_label": REJECT_LABELS.get(job.reject_reason) if job.reject_reason else None,
        "reject_note": job.reject_note,
        "fill_notes": read("fill_notes.md"),
        "error": read("error.txt"),
        "history": _history(app_dir),
    }


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
    """Checkpoint 1 passed. Only an approved job may reach the fill loop."""
    job, app_dir = _job_and_dir(job_id)
    if job.status != Status.AWAITING_REVIEW:
        raise HTTPException(409, f"job is {job.status.value}, not awaiting review")
    store.set_status(app_dir, Status.APPROVED, decision.note)
    queue.update(job_id, status=Status.APPROVED)
    return {"id": job_id, "status": Status.APPROVED.value}


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
    if job.status != Status.FILLED:
        raise HTTPException(409, f"job is {job.status.value}, not filled")
    store.set_status(app_dir, Status.SUBMITTED, decision.note or "submitted by hand")
    queue.update(job_id, status=Status.SUBMITTED)
    return {"id": job_id, "status": Status.SUBMITTED.value}


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
