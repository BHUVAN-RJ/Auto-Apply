"""FastAPI app: queue intake from the capture extension, plus the review UI.

Runs on localhost:8787. CORS is open to the extension only in the sense that
the server binds to loopback -- nothing outside this machine can reach it.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import queue, runner
from .models import Job, Status
from .review import router as review_router
from .screen import router as screen_router
from .settings import router as settings_router

ROOT = Path(__file__).resolve().parent.parent
REVIEW_DIR = ROOT / "review"

app = FastAPI(title="job-autopilot", version="0.1.0")

# The extension posts from whatever origin the job page is on, so the origin
# cannot be pinned. Safe because the server binds to 127.0.0.1 only.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


class CaptureRequest(BaseModel):
    url: str
    title: str = ""
    source: str = ""
    company: str = ""


class StatusUpdate(BaseModel):
    status: Status


app.include_router(review_router)
app.include_router(screen_router)
app.include_router(settings_router)


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.post("/capture")
def capture(req: CaptureRequest) -> dict:
    """Called by the capture extension's context-menu item.

    Capturing is the instruction to tailor. The pipeline starts straight
    away, detached, so by the time the review tab is opened the posting is
    scraped and the resume and letter are ready or on their way. Nothing
    past checkpoint 1 starts here; the fill still waits for approval.
    """
    job, created = queue.add(Job(**req.model_dump()))
    pid = None
    if job.status == Status.QUEUED:
        # A re-captured URL that never got processed is started too, rather
        # than answering "already queued" and leaving it there.
        pid = runner.start_pipeline(job.id)
    return {"id": job.id, "created": created, "queued": len(queue.pending()),
            "processing": bool(pid)}


@app.post("/jobs/{job_id}/process")
def process_now(job_id: str) -> dict:
    """Start (or restart) the pipeline for a job that has not been tailored.

    For a capture that happened while the server was down, or a run that
    failed on a transient fetch. A job already through checkpoint 1 is
    refused: re-tailoring is the review page's revise action.
    """
    job = queue.get(job_id)
    if job is None:
        raise HTTPException(404, "unknown job")
    if job.status not in (Status.QUEUED, Status.FAILED, Status.TAILORING):
        raise HTTPException(409, f"job is {job.status.value}; use revise instead")
    pid = runner.start_pipeline(job_id)
    if not pid:
        raise HTTPException(500, "could not start pipeline.py")
    return {"id": job_id, "pid": pid}


@app.get("/jobs")
def jobs() -> list[dict]:
    return [j.model_dump(mode="json") | {"id": j.id} for j in queue.all_jobs()]


@app.get("/jobs/{job_id}")
def job_detail(job_id: str) -> dict:
    job = queue.get(job_id)
    if job is None:
        raise HTTPException(404, "unknown job")
    return job.model_dump(mode="json") | {"id": job.id}


@app.post("/jobs/{job_id}/status")
def set_status(job_id: str, update: StatusUpdate) -> dict:
    job = queue.update(job_id, status=update.status)
    if job is None:
        raise HTTPException(404, "unknown job")
    return job.model_dump(mode="json") | {"id": job.id}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(REVIEW_DIR / "index.html")
