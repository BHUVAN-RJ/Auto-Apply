"""FastAPI app: queue intake from the capture extension, plus the review UI.

Runs on localhost:8787. CORS is open to the extension only in the sense that
the server binds to loopback -- nothing outside this machine can reach it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from archive import store

from . import postings, queue, runner, seen, settings
from .models import Job, Status
from .form import router as form_router
from .profile import router as profile_router
from .review import router as review_router
from .screen import router as screen_router
from .settings import router as settings_router
from .voice import router as voice_router

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
    # The page's rendered text, when the capture comes from the page itself.
    # Kept for the pipeline in case the fetch sees only a client-side shell.
    text: str = ""
    # The banner's auto-approve box. None: whatever was set for this posting
    # on Jobright's page (`/prefs`), else the global switch.
    auto_fill: Optional[bool] = None


class PostingRequest(BaseModel):
    """Jobright's own copy of a posting, keyed by its posting id."""

    id: str
    url: str
    title: str = ""
    text: str
    company: str = ""


class StatusUpdate(BaseModel):
    status: Status


app.include_router(review_router)
app.include_router(screen_router)
app.include_router(settings_router)
app.include_router(profile_router)
app.include_router(form_router)
app.include_router(voice_router)


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.post("/posting")
def posting(req: PostingRequest) -> dict:
    """Keep a source site's copy of a posting, for the pipeline's fallback.

    Some Apply buttons land on a bare application form; this text is what
    the tailor gets then. Nothing is queued here.
    """
    postings.save_source(req.id, postings.Saved(url=req.url, title=req.title,
                                                text=req.text, company=req.company))
    return {"ok": True}


class QueuedRequest(BaseModel):
    jr_id: str


@app.post("/queued")
def queued(req: QueuedRequest) -> dict:
    """Whether the employer page Jobright opened for this posting has queued
    itself yet. The banner on Jobright's page asks so its close countdown
    starts only once the job is actually in; before that it stays put."""
    job = next((j for j in queue.all_jobs() if postings.source_id(j.url) == req.jr_id), None)
    return {"queued": job is not None, "id": job.id if job else None}


@app.post("/capture")
def capture(req: CaptureRequest) -> dict:
    """Called by the capture extension's context-menu item.

    Capturing is the instruction to tailor. The pipeline starts straight
    away, detached, so by the time the review tab is opened the posting is
    scraped and the resume and letter are ready or on their way. Nothing
    past checkpoint 1 starts here; the fill still waits for approval.
    """
    if seen.is_source_page(req.url):
        # A listing site's page is not a job. Its Apply leads to one.
        return {"id": None, "created": False, "reason": "source_page",
                "queued": len(queue.pending()), "processing": False}
    auto_fill = req.auto_fill
    if auto_fill is None:
        auto_fill = AUTO_FILL_PREFS.pop(postings.source_id(req.url) or "", None)
    job, created = queue.add(Job(**req.model_dump(exclude={"text", "auto_fill"}), auto_fill=auto_fill))
    if not created and auto_fill is not None and job.auto_fill != auto_fill:
        job = queue.update(job.id, auto_fill=auto_fill) or job
    if req.text.strip():
        postings.save_captured(job.id, postings.Saved(url=req.url, title=req.title,
                                                      text=req.text, company=req.company))
    pid = None
    if job.status == Status.QUEUED:
        # A re-captured URL that never got processed is started too, rather
        # than answering "already queued" and leaving it there.
        pid = runner.start_pipeline(job.id)
    return {"id": job.id, "created": created, "queued": len(queue.pending()),
            "processing": bool(pid)}


# Unchecked auto-approve on Jobright's posting page, by posting id: the
# employer tab it opens queues the job and picks this up. In memory only;
# it is a click seconds earlier, not a setting.
AUTO_FILL_PREFS: dict[str, bool] = {}


class PrefRequest(BaseModel):
    jr_id: str
    auto_fill: bool


@app.post("/prefs")
def prefs(req: PrefRequest) -> dict:
    AUTO_FILL_PREFS[req.jr_id] = req.auto_fill
    return {"ok": True}


class AutofilledRequest(BaseModel):
    url: str
    note: str = ""


def wants_auto_fill(job: Job) -> bool:
    """The job's own auto-approve answer, else the global switch."""
    return settings.auto_fill() if job.auto_fill is None else bool(job.auto_fill)


@app.post("/autofilled")
def autofilled(req: AutofilledRequest) -> dict:
    """The injector has finished Jobright's autofill in a tab (or found no
    button there). This is the trigger for the browser agent on a job that
    is already in autopilot: a job approved, filled, or failed mid-fill gets
    its fill (re)started in that tab, with the switch on. A job the
    pipeline is still working on waits for the pipeline, which starts the
    fill itself; a job not in autopilot at all is the banner's to capture.
    Nothing here submits, and a running fill is never doubled."""
    match = seen.find(req.url)
    if match is None or match.level != "high":
        return {"action": "none", "id": None}
    job = queue.get(match.id)
    if job is None:
        return {"action": "none", "id": None}
    if not wants_auto_fill(job):
        return {"action": "waiting", "id": job.id, "status": job.status.value}
    app_dir = Path(job.app_dir) if job.app_dir else None
    has_resume = bool(app_dir and (app_dir / "resume.pdf").exists())
    if job.status in (Status.APPROVED, Status.FILLING, Status.FILLED, Status.FAILED) and has_resume:
        if runner.fill_pid(job.id):
            return {"action": "running", "id": job.id, "status": job.status.value}
        # Back to APPROVED so apply.py picks it up (it only touches approved jobs).
        store.set_status(app_dir, Status.APPROVED, f"fill restarted after autofill ({req.note or 'no note'})")
        queue.update(job.id, status=Status.APPROVED, error=None)
        pid = runner.launch("apply.py", job.id)
        return {"action": "fill" if pid else "waiting", "id": job.id, "pid": pid}
    if job.status in (Status.QUEUED, Status.FAILED) and not has_resume:
        pid = runner.start_pipeline(job.id)
        return {"action": "pipeline" if pid else "waiting", "id": job.id, "pid": pid}
    return {"action": "waiting", "id": job.id, "status": job.status.value}


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
