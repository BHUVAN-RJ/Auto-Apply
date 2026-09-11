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

from . import queue
from .models import Job, Status
from .review import router as review_router

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


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.post("/capture")
def capture(req: CaptureRequest) -> dict:
    """Called by the capture extension's context-menu item."""
    job, created = queue.add(Job(**req.model_dump()))
    return {"id": job.id, "created": created, "queued": len(queue.pending())}


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
