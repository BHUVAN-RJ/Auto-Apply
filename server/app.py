"""FastAPI app: queue intake from the capture extension, plus the review UI.

Runs on localhost:8787. CORS is open to the extension only in the sense that
the server binds to loopback -- nothing outside this machine can reach it.
"""

from __future__ import annotations

from pathlib import Path
import time
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from archive import store
from tailor import llm

from . import postings, queue, runner, seen, settings, watch
from .models import Job, Status
from .form import router as form_router
from .profile import router as profile_router
from .prompts import router as prompts_router
from .report import router as report_router

# The identity service's own router: a lookup, a backfill, the stats and the
# collision report. Read-only on the queue and the applications, and the
# same ladder `seen.find` now answers with, exposed so the collection
# server (and a terminal) can ask the one question that must never be
# guessed: have I applied to this already?
from identity.api import router as identity_router
from .github import router as github_router
from .keys import router as keys_router
from .outreach import router as outreach_router
from .review import router as review_router, start_router
from .scout import router as scout_router
from .screen import router as screen_router
from .screening import router as screening_router
from .simplify import router as simplify_router
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
    allow_methods=["GET", "POST", "PATCH"],
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
    # The banner's "Use Opus" button. None: whatever was pressed for this
    # posting on Jobright's page (`/prefs`), else the cheap default.
    premium: Optional[bool] = None


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
app.include_router(outreach_router)
app.include_router(screen_router)
app.include_router(screening_router)
app.include_router(simplify_router)
app.include_router(settings_router)
app.include_router(keys_router)
app.include_router(profile_router)
app.include_router(form_router)
app.include_router(voice_router)
app.include_router(github_router)
app.include_router(scout_router)
app.include_router(prompts_router)
app.include_router(report_router)
app.include_router(identity_router)
# Top level rather than under /review: the banner on a posting's own page
# presses it, and the page is asking "start this job now", not asking about a
# job it is already reading.
app.include_router(start_router)


@app.exception_handler(queue.Ambiguous)
def _ambiguous_job_id(request, error: queue.Ambiguous):
    """A four-hex id that names two jobs is answered, never guessed.

    Every handler that takes a `{job_id}` reaches `queue.get` sooner or later,
    and the one thing that must not happen is acting on whichever row came
    first in the file (KNOWN_ISSUES #1: a submitted mark aimed at a queued
    Zoox row kept landing on a skipped Dayforce row). `server/review.py`
    answers this itself, with the posting's URL as the tie-breaker where the
    page can supply one; this is the floor under every other route.
    """
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=409, content={"detail": {
        "error": "ambiguous_id", "id": error.job_id, "message": str(error),
        "candidates": error.candidates()}})


@app.on_event("startup")
def _start_watch() -> None:
    # The first start after an update: the person's prompt edits merged
    # onto the new stock text, before any job reads them.
    from tailor import prompts
    for row in prompts.sync():
        state = "merged" if row["merged"] else "conflict, their version kept"
        print(f"prompt {row['name']}: {state}")
    # The server's own look at every filled form: form state for the
    # correction loop, and submitted when the page turns into a
    # confirmation. Independent of the script in the tab.
    watch.start()
    # The scout: every watched careers page read on its schedule, new
    # roles at the level recorded and mailed. Nothing queued by itself.
    from scout import run as scout_run
    scout_run.start()
    # One tab at a time: while the hold is on nothing opens, and when it
    # comes off the approved jobs are filled one after another.
    from server import runner as job_runner
    job_runner.start_serial()
    # The clock behind a scheduled follow-up: an approved email waits for
    # its hour and goes then, one a minute. It sends nothing a click did
    # not already approve.
    from server import sendlater
    sendlater.start()
    # Every application already submitted taken through the follow-up step,
    # one at a time, until none is left. A new submission prepares itself.
    from server import followqueue
    followqueue.start()


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
    premium = req.premium
    source_id = postings.source_id(req.url) or ""
    if auto_fill is None:
        auto_fill = AUTO_FILL_PREFS.pop(source_id, None)
    if premium is None:
        premium = PREMIUM_PREFS.pop(source_id, None)
    # The resolved model name travels on the row, not the flag: the folder
    # should say what it was written with even if the setting moves later.
    tailor_model = llm.premium_model() if premium else None
    job, created = queue.add(Job(**req.model_dump(exclude={"text", "auto_fill", "premium"}),
                                 auto_fill=auto_fill, tailor_model=tailor_model))
    if not created and auto_fill is not None and job.auto_fill != auto_fill:
        job = queue.update(job.id, auto_fill=auto_fill) or job
    if not created and premium is not None and job.tailor_model != tailor_model:
        job = queue.update(job.id, tailor_model=tailor_model) or job
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


PREMIUM_PREFS: dict[str, bool] = {}


# The person pressed Add on Jobright's posting page, by posting id and when.
#
# Why this exists at all (2026-10-30): the countdown that used to queue a job
# by itself is gone, and `/capture` refuses a `jobright.ai` URL outright, so
# the only queueable URL is the employer's and the only way to reach it is
# Jobright's own Apply. Without a channel between the two tabs, one press on
# Jobright became two - one there, one on the page it opened - which is worse
# than the thing that was removed. This is the channel, and it carries one
# fact: *the person asked for this posting*. It is not auto-queueing coming
# back; it is a click seconds earlier, in another tab, being honoured.
ASKED_PREFS: dict[str, float] = {}

# How long that press means anything. A decision does not keep: somebody who
# pressed Add an hour ago and has since wandered onto another employer's page
# in a tab that still carries the tag has not asked for *that* job.
ASKED_TTL = 300.0


def asked_for(jr_id: str) -> bool:
    """Did the person press Add for this posting, recently, and not yet
    spent it? Reading it consumes it, so one press queues one job."""
    when = ASKED_PREFS.pop(jr_id, None)
    if when is None:
        return False
    return (time.monotonic() - when) <= ASKED_TTL


class PrefRequest(BaseModel):
    jr_id: str
    auto_fill: Optional[bool] = None
    premium: Optional[bool] = None
    asked: Optional[bool] = None


@app.post("/prefs")
def prefs(req: PrefRequest) -> dict:
    """What was pressed on Jobright's posting page, for the employer tab its
    Apply opens: that tab is the one that queues the job."""
    if req.auto_fill is not None:
        AUTO_FILL_PREFS[req.jr_id] = req.auto_fill
    if req.premium is not None:
        PREMIUM_PREFS[req.jr_id] = req.premium
    if req.asked:
        ASKED_PREFS[req.jr_id] = time.monotonic()
    return {"ok": True}


class AskedRequest(BaseModel):
    jr_id: str


@app.post("/asked")
def asked(req: AskedRequest) -> dict:
    """Was Add pressed for this posting on Jobright? Answered once.

    The employer tab asks this of itself: yes, and it queues the job the
    way the countdown used to - because the press has already happened, on
    the posting, by the person. No, and it waits for a click, which is every
    other page in the browser.
    """
    return {"asked": asked_for(req.jr_id)}


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
    """Every row, with the moment a decided one was decided.

    `added_at` is when the posting was noticed; the closed shelves are read
    as a record of what was sent and when, and two applications can go out
    on one day, so each decided row carries the time it reached that status
    off its own folder's history.
    """
    from archive import store
    from outreach import store as outreach_store
    from server.models import Status

    # Submitting is not the end of the job: the follow-up emails are written
    # by the submission itself and then wait for a click, so the list needs
    # to know which applications still have one. A submitted row always
    # carries this key, empty when nothing has been prepared, because
    # "prepared nothing" and "never asked" look the same on a missing key.
    outreach = outreach_store.summaries()
    blank = {"prepare_status": "", "total": 0, "sent": 0, "unsent": 0,
             "contacts": 0, "note": "", "error": ""}
    out = []
    for job in queue.all_jobs():
        row = job.model_dump(mode="json") | {"id": job.id}
        if job.id in outreach:
            row["outreach"] = outreach[job.id]
        elif job.status is Status.SUBMITTED:
            row["outreach"] = dict(blank)
        wanted = (Status.SUBMITTED if job.status is Status.SUBMITTED
                  else Status.SKIPPED if job.status is Status.SKIPPED else None)
        if wanted and job.app_dir:
            row["decided_at"] = store.reached_at(Path(job.app_dir), wanted) or job.added_at
        # A job waiting on a prompt pasted into a chat is not waiting on the
        # person's review; it is waiting on them somewhere else entirely, so
        # the list puts it at the bottom of what needs them rather than at
        # the top (2026-09-30).
        if job.app_dir:
            row["handoff"] = (Path(job.app_dir) / "handoff.md").exists()
        out.append(row)
    return out


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
    # One file, no build step, edited often: Chrome's heuristic cache kept
    # an old copy for a whole session once. Always revalidate.
    return FileResponse(REVIEW_DIR / "index.html", headers={"Cache-Control": "no-cache"})
