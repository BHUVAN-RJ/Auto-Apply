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
import logging
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel

import paths
from archive import store
from browser import chrome, guard
from identity import keys as identity_keys
from server import corrections, queue, runner, seen, thread as job_thread
from server.models import REJECT_LABELS, Job, RejectReason, Status, utcnow
from tailor import answers, llm, profile, tailor

router = APIRouter(prefix="/review", tags=["review"])

# Not `log`: this module already serves a job's apply log under that name.
logger = logging.getLogger(__name__)

# Where apply.py writes its per-job logs.
DATA_DIR = paths.DATA

# Artifacts the review page may request. An allow-list rather than a path join,
# so a crafted name cannot walk out of the application folder.
ARTIFACTS = {
    "posting.md": "text/markdown",
    "resume.tex": "text/plain",
    "resume.diff": "text/plain",
    "suggestions.md": "text/markdown",
    "mismatch.md": "text/markdown",
    "thin_posting.md": "text/markdown",
    "screen.json": "application/json",
    "rejection.md": "text/markdown",
    "fill_notes.md": "text/markdown",
    "answers.md": "text/markdown",
    "error.txt": "text/plain",
    "resume.pdf": "application/pdf",
    "resume_changes.pdf": "application/pdf",
    "resume_both.pdf": "application/pdf",
    "cover_letter.md": "text/markdown",
    "cover_letter.tex": "text/plain",
    "cover_letter.pdf": "application/pdf",
    "cover_letter_error.txt": "text/plain",
    "fill_screenshot.png": "image/png",
}


class Decision(BaseModel):
    """A human decision about one job.

    `url` is the page the person was standing on when they pressed. It is
    not decoration: `Job.id` is four hex characters and can name two jobs
    (KNOWN_ISSUES #1), so the URL is what says which of them the page meant.
    """

    note: Optional[str] = None
    url: Optional[str] = None


class FillRequest(BaseModel):
    """`force` is the second gate: the page asks the human before sending it."""

    note: Optional[str] = None
    force: bool = False


class Rejection(BaseModel):
    """A rejection always carries a reason. The note is for the specifics."""

    reason: RejectReason
    note: Optional[str] = None


class Question(BaseModel):
    question: str


class Revision(BaseModel):
    instruction: str


class ThreadMessage(BaseModel):
    """One message in a job's thread: a question to answer, or a change to
    make. `keep_status` is the re-tailor that leaves a filled or submitted
    job where it stands and only writes new documents."""

    text: str
    action: str = "ask"          # ask | change
    keep_status: bool = True
    # A change asked for on the expensive model. The choice sticks to the
    # row, so the job stays an Opus job for anything it is re-tailored with
    # later; it is the same decision the banner's button makes at capture.
    premium: bool = False


def _same_posting(rows: list[Job], url: str) -> Optional[Job]:
    """Which of these colliding rows is the page at `url`.

    Two rows share a short id only by hash collision, so they are two
    different postings and the URL tells them apart. The URL is matched with
    the app's own machinery and nothing new: `identity.keys.canonical` first
    (the same canonical form `seen.same_job` and the index are keyed on), and
    then `seen.same_job`, which is the proof-only half of the
    never-applied-twice ladder - a posting reached by another one of its
    links is still that posting. Resemblance is deliberately not consulted
    here: it is for a person reading a banner, never for choosing which job
    a click lands on.
    """
    if not url:
        return None
    want = identity_keys.canonical(url)
    exact = [row for row in rows if identity_keys.canonical(row.url) == want]
    if len(exact) == 1:
        return exact[0]
    asking = Job(url=url)
    proven = [row for row in rows if seen.same_job(row, asking)]
    return proven[0] if len(proven) == 1 else None


def _by_url(url: str) -> Optional[Job]:
    """The job this page is, through the one never-applied-twice ladder.

    `seen.find` already answers "have I applied to this?" for the banner,
    the review page, `/screen` and the queue, so there is no second matcher
    here. Only a **certain** match counts: a likely one is "same company,
    similar title", which at Palantir is eleven separate openings, and
    acting on one of those would fill the wrong job's form or mark the wrong
    application sent.
    """
    if not url:
        return None
    match = seen.find(url)
    if match is None or match.level != seen.CERTAIN_LEVEL:
        return None
    rows = queue.rows_for(match.id)
    if len(rows) == 1:
        return rows[0]
    return _same_posting(rows, url)


def _ambiguous(job_id: str, rows: list[Job]) -> HTTPException:
    """409, saying it is ambiguous and naming the jobs it is between.

    Never a guess and never a bare 404: the person asked about a job that
    exists, and the only honest answer is that the id names two of them.
    """
    return HTTPException(409, {
        "error": "ambiguous_id",
        "id": job_id,
        "message": f"the id {job_id} names {len(rows)} jobs; open the one you mean "
                   f"from the list, or send the posting's URL with the request",
        "candidates": [{"id": row.id, "url": row.url, "title": row.title,
                        "company": row.company, "status": row.status.value,
                        "added_at": row.added_at} for row in rows],
    })


def _resolve_job(job_id: str, url: str = "") -> Optional[Job]:
    """One job for this request, or None. Raises 409 on an ambiguous id.

    The id is tried first, because every link the page writes carries one.
    When it names two rows the URL breaks the tie; with no URL to break it,
    the request is refused rather than aimed at whichever row came first in
    the file (`queue.Ambiguous`). An id that names nothing falls back to the
    URL, which is how "I submitted it" still reaches the job when the page
    is sending a posting we know under a different link.
    """
    rows = queue.rows_for(job_id) if job_id else []
    if len(rows) == 1:
        return rows[0]
    if len(rows) > 1:
        picked = _same_posting(rows, url)
        if picked is not None:
            return picked
        raise _ambiguous(job_id, rows)
    return _by_url(url)


def _job_and_dir(job_id: str, url: str = "") -> tuple[Job, Path]:
    job = _resolve_job(job_id, url)
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
        # The page was a sign-in wall or a bare form, so nothing was tailored.
        "thin_posting": read("thin_posting.md"),
        # The fill found no form: the page keeps it behind Apply and a
        # sign-in. Not an error, and not a reason to close the job.
        "needs_sign_in": read("needs_sign_in.txt"),
        # A job captured with "Use Opus": the prompt is written and waiting
        # to be copied, and there are no documents until the person brings
        # the resume back. The text itself is fetched separately - it is the
        # whole master resume plus the posting, and the list polls `detail`.
        "handoff": (app_dir / "handoff.md").exists(),
        # Are there documents to attach? The Try again button says which
        # step it will restart from, and a job with a compiled resume is
        # retried at the fill rather than at the tailor.
        "tailored": _tailored(app_dir),
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
        "answers": read("answers.md"),
        "questions": _open_questions(app_dir),
        "thread": job_thread.load(job.id),
        "thread_running": job_thread.running(job.id),
        "changes": corrections.changes(app_dir),
        "stories_used": [line for line in read("stories_used.txt").splitlines() if line.strip()],
        "error": read("error.txt"),
        "error_detail": job.error,
        "log": _log_path(job.id),
        "history": _history(app_dir),
        "fill": _fill_state(job.id),
    }


def _open_questions(app_dir: Path) -> list[str]:
    """Free-form questions the filled form still has empty, off the latest
    look at it (the tab's own pings, else the fill's snapshot)."""
    state = corrections._read(app_dir / corrections.FORM_STATE).get("fields")
    if not state:
        state = corrections._read(app_dir / corrections.FILL_REPORT).get("after_agent")
    return answers.open_questions(state or {})


def _answer(app_dir: Path, question: str, history: str = "") -> answers.Answer:
    """One answer from everything this application has: the posting, the
    tailored resume, the cover letter, the profile with the same stories
    the tailor read, the applicant facts, and what the thread already
    said."""
    context = answers.Context.from_app_dir(
        app_dir, profile=tailor.load_profile(stories=profile.read_used(app_dir)),
        applicant=profile.applicant_facts() or "")
    context.thread = history
    try:
        result = answers.answer(question, context)
    except guard.ProtectedField:
        raise HTTPException(422, "That is a visa / work-authorisation question; it is yours to answer.")
    except answers.AnswerError as error:
        raise HTTPException(502, str(error))
    store.write_or_append(app_dir, "answers.md",
                          f"# Asked on the review page\n\n**{question}**\n\n{result.text}\n")
    return result


@router.post("/{job_id}/ask")
def ask(job_id: str, body: Question) -> dict:
    """One free-form answer, kept in answers.md with the ones the fill
    produced; typed into the form by the human."""
    _, app_dir = _job_and_dir(job_id)
    question = " ".join(body.question.split())
    if not question:
        raise HTTPException(400, "question is empty")
    result = _answer(app_dir, question)
    return {"id": job_id, "question": result.question, "answer": result.text, "model": result.model}


@router.get("/{job_id}/thread")
def read_thread(job_id: str) -> dict:
    """The conversation about this job. Polled while a change runs."""
    _resolve_job(job_id) or _job_and_dir(job_id)
    return {"id": job_id, "turns": job_thread.load(job_id), "running": job_thread.running(job_id)}


@router.post("/{job_id}/thread")
def say(job_id: str, body: ThreadMessage) -> dict:
    """A message in the job's thread: answered, or tailored into new
    documents. A change never approves anything and never starts a fill."""
    job, app_dir = _job_and_dir(job_id)
    text = body.text.strip()
    if not text:
        raise HTTPException(400, "the message is empty")
    if body.action not in ("ask", "change"):
        raise HTTPException(422, f"no such action {body.action!r}")

    if body.action == "change":
        from pipeline import retailor  # lazy: pipeline imports the server package

        if body.premium:
            model = llm.premium_model()
            if job.tailor_model != model:
                job = queue.update(job.id, tailor_model=model) or job
        try:
            job_thread.start_change(job_id, text,
                                    lambda: retailor(job, text, keep_status=body.keep_status).name)
        except job_thread.ThreadError as exc:
            raise HTTPException(409, str(exc))
        return {"id": job_id, "turns": job_thread.load(job_id), "running": True}

    question = " ".join(text.split())
    history = job_thread.history(job_id)
    job_thread.add(job_id, "question", question)
    try:
        result = _answer(app_dir, question, history)
    except HTTPException as exc:
        job_thread.add(job_id, "error", str(exc.detail))
        raise
    job_thread.add(job_id, "answer", result.text, model=result.model)
    return {"id": job_id, "turns": job_thread.load(job_id), "running": False}


@router.post("/{job_id}/reveal")
def reveal(job_id: str) -> dict:
    """Open the application folder in Finder with the resume selected.

    The resume alone (2026-10-03, asked for): selecting the letter beside it
    meant two highlighted files to pick between at the moment of dragging
    one onto a slot, and the resume is the file that is always wanted. The
    letter is made under its upload name in the same folder, a row away.
    """
    import subprocess
    import sys

    import apply as apply_script  # lazy: apply.py pulls in the browser stack

    _, app_dir = _job_and_dir(job_id)
    # The named copies are made by the fill; a folder that was only
    # tailored has none yet, and the upload name is half the point of
    # dragging the file in by hand.
    # Both named copies are made, because the fill may not have run yet and
    # the upload name is half the point of dragging a file in by hand; only
    # the resume is selected.
    letter = apply_script.upload_copy(app_dir, "cover_letter.pdf",
                                      apply_script.cover_letter_filename())
    files = [p for p in (apply_script.upload_copy(app_dir, "resume.pdf"),) if p is not None]
    if sys.platform != "darwin":
        return {"id": job_id, "folder": str(app_dir), "revealed": [], "opened": False,
                "note": "Finder is macOS only; the folder is above."}
    if files:
        items = ", ".join(f'POSIX file "{p}"' for p in files)
        script = f'tell application "Finder" to reveal {{{items}}}\ntell application "Finder" to activate'
        done = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
        if done.returncode == 0:
            return {"id": job_id, "folder": str(app_dir), "opened": True,
                    "revealed": [p.name for p in files],
                    "also": letter.name if letter else ""}
    # No named copies yet, or Finder refused: the folder itself will do.
    subprocess.run(["open", str(app_dir)], capture_output=True, text=True)
    return {"id": job_id, "folder": str(app_dir), "opened": True,
            "revealed": [p.name for p in files], "also": letter.name if letter else ""}


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
    # A re-tailor writes a new folder under the same URL, so nothing here
    # may be cached: Chrome's PDF viewer will otherwise keep showing the
    # resume it already rendered.
    headers = {"Cache-Control": "no-store"}
    if ARTIFACTS[name].startswith("text/"):
        return PlainTextResponse(path.read_text(errors="replace"), headers=headers)
    return FileResponse(path, media_type=ARTIFACTS[name], headers=headers)


# What the tailor model is told when the reviewer approves a job it called a
# poor fit. The verdict is a recommendation; approval is the answer to it.
# The text lives with the tailor, beside the one the pipeline sends itself.
OVERRULE = tailor.OVERRULE


def _tailored(app_dir: Path) -> bool:
    """Does this folder hold documents to upload? A poor-fit run writes
    `mismatch.md` and nothing else, and a fill there has nothing to attach."""
    return (app_dir / "resume.pdf").exists()


def _overrule_mismatch(job: Job, job_id: str) -> dict:
    """Tailor an untailored job, then fill it, off the request thread.

    A poor-fit verdict stops at checkpoint 1 with no documents. Approving or
    filling it anyway used to launch `apply.py` against an empty folder, which
    skipped with "resume.pdf is missing" and left the job sitting at
    `approved` with nothing to show. The documents are written first, in the
    job's own thread so the page can watch it, and the fill follows them.
    """
    from pipeline import retailor  # lazy: pipeline imports the server package

    def work() -> str:
        app_dir = retailor(job, OVERRULE, keep_status=True)
        pid = runner.start_fill(job_id)
        if pid:
            store.set_status(app_dir, Status.APPROVED, f"filling started (pid {pid})")
        return app_dir.name

    try:
        job_thread.start_change(job_id, OVERRULE, work)
    except job_thread.ThreadError as exc:
        raise HTTPException(409, str(exc))
    return {"id": job_id, "status": Status.APPROVED.value, "filling": False,
            "pid": None, "tailoring": True}


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

    if not _tailored(app_dir):
        return _overrule_mismatch(job, job_id)

    pid = runner.start_fill(job_id)
    if pid:
        store.set_status(app_dir, Status.APPROVED, f"filling started (pid {pid})")
    elif runner.held():
        store.set_status(app_dir, Status.APPROVED, "approved; waiting for the fills to be let go")
    return {"id": job_id, "status": Status.APPROVED.value, "filling": bool(pid),
            "pid": pid, "held": runner.held() and not pid}


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
    if not _tailored(app_dir):
        # Nothing was tailored (a poor-fit verdict). Write the documents
        # first; the fill starts when they land.
        return _overrule_mismatch(job, job_id)
    running = runner.fill_pid(job_id)
    if running and not request.force:
        raise HTTPException(
            409, {"error": "fill_running", "pid": running,
                  "message": f"a fill is already running for this job (pid {running})"},
        )
    # One tab at a time, across jobs as well as within one. Two fills share
    # the one Chrome window the app owns and open their tabs on top of each
    # other; this is the same rule the hold enforces in bulk.
    other = runner.any_fill_running()
    if other and other != job_id and not request.force:
        raise HTTPException(
            409, {"error": "another_fill_running", "job": other,
                  "message": "another job is being filled right now; let it finish, "
                             "or force this one to take the browser"},
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

    A rejection stops the work at once. The fill goes (2026-09-30): a
    rejected job whose `apply.py` kept running held the one tab the app
    owns, so `runner.serial_tick` started nothing and nine approved jobs
    sat still behind one job the person had already said no to - and the
    agent itself, with no form left to work on, once wandered into another
    job's tab. Its tab goes with it, the same way a submission's does: the
    form is finished with either way, and only that tab closes.
    """
    job, app_dir = _job_and_dir(job_id)
    label = REJECT_LABELS[rejection.reason]
    detail = f"{label}: {rejection.note}" if rejection.note else label
    killed = runner.stop_fill(job_id)
    if killed:
        detail += f" (fill {killed} killed)"
    tailoring = runner.stop_pipeline(job_id)
    if tailoring:
        detail += f" (tailoring {tailoring} stopped)"

    store.set_status(app_dir, Status.SKIPPED, detail)
    store.write_or_append(app_dir, "rejection.md", f"# Rejected\n\n**{label}**\n\n"
                          f"{rejection.note or '_no further detail_'}\n")
    queue.update(
        job_id,
        status=Status.SKIPPED,
        reject_reason=rejection.reason,
        reject_note=rejection.note,
    )
    tab = "closed" if chrome.close_tab(_form_tab(job, app_dir)) else "not_open"
    return {"id": job_id, "status": Status.SKIPPED.value, "reason": label,
            "killed": killed, "tab": tab}


# Everything a retry may be started from: a run that fell over, a job the
# person rejected and has changed their mind about, and the live states a run
# can die in the middle of. SUBMITTED is the one that is not here - the
# application has gone, the row is the record of it, and the way to work on
# it again is the thread's re-tailor.
RETRYABLE = (Status.QUEUED, Status.TAILORING, Status.AWAITING_REVIEW,
             Status.APPROVED, Status.FILLING, Status.FILLED, Status.FAILED,
             Status.SKIPPED)


@router.post("/{job_id}/retry")
def retry(job_id: str) -> dict:
    """Start this job again from wherever it has nothing.

    A job can stop for a reason that has nothing to do with the job: the key
    ran out of credit (`OpenRouter returned 402: Insufficient credits`, which
    is what prompted this), a fetch timed out, the socket dropped. The row
    went to `failed`, which lives on a closed shelf with no buttons on it, so
    a perfectly good posting was unreachable - there was nothing to press
    (2026-10-09, asked for).

    From the top means from the first step that has nothing to show:

    - no `resume.pdf` in the folder, so there is nothing to attach: the
      pipeline runs again, which screens, fetches, tailors and compiles, and
      the job comes back to checkpoint 1 for the person as usual.
    - documents already there: the fill runs again instead, because
      re-tailoring a resume that compiled is spending a model call to get the
      same thing twice.

    A retry is a restart, so whatever is still running for this job is
    stopped first. Nothing here submits, nothing is approved on the person's
    behalf past checkpoint 1 that was not approved before, and a held queue
    still holds: the fill is handed to `runner.start_fill`, which is the one
    gate that decides whether a tab may open now.
    """
    job, app_dir = _job_and_dir(job_id)
    if job.status == Status.SUBMITTED:
        raise HTTPException(409, "this application has been submitted; "
                                 "ask for a change in the thread instead")
    if job.status not in RETRYABLE:
        raise HTTPException(409, f"job is {job.status.value} and cannot be retried")

    was = job.status.value
    note = f"retried from {was}"
    killed = runner.stop_fill(job_id)
    if killed:
        note += f" (fill {killed} stopped)"
    tailoring = runner.stop_pipeline(job_id)
    if tailoring:
        note += f" (tailoring {tailoring} stopped)"
    # The rejection is lifted, because the job is being tried again: a row
    # still carrying "poor fit" while it tailors says two things at once.
    if job.reject_reason or job.reject_note:
        note += " (rejection lifted)"

    if _tailored(app_dir):
        # `apply.py` only touches an approved job, and the error has to go
        # with it: a rerun that works still shows the old failure otherwise.
        store.set_status(app_dir, Status.APPROVED, note)
        queue.update(job_id, status=Status.APPROVED, error=None,
                     reject_reason=None, reject_note=None)
        pid = runner.start_fill(job_id, force=True)
        if pid:
            store.set_status(app_dir, Status.APPROVED, f"filling started (pid {pid})")
        elif runner.held():
            store.set_status(app_dir, Status.APPROVED,
                             "retried; waiting for the fills to be let go")
        return {"id": job_id, "was": was, "step": "fill", "status": Status.APPROVED.value,
                "filling": bool(pid), "pid": pid, "held": runner.held() and not pid,
                "killed": killed}

    store.set_status(app_dir, Status.QUEUED, f"{note}; tailoring again")
    queue.update(job_id, status=Status.QUEUED, error=None,
                 reject_reason=None, reject_note=None)
    pid = runner.start_pipeline(job_id)
    if not pid:
        raise HTTPException(500, "could not start pipeline.py")
    return {"id": job_id, "was": was, "step": "tailor", "status": Status.QUEUED.value,
            "filling": False, "pid": pid, "killed": killed}


@router.get("/meta/reject-reasons")
def reject_reasons() -> list[dict]:
    """The reason list the review page offers."""
    return [{"value": reason.value, "label": label} for reason, label in REJECT_LABELS.items()]


def _prepare_follow_up(job_id: str) -> None:
    """Start the follow-up drafts for a job that has just been submitted.

    Imported here rather than at module scope, the way `pipeline` is: the
    outreach package reaches `apply.upload_copy` for the named resume copy,
    and that side of the tree imports the server package back.

    Nothing about a submission may depend on this working. `follow.start`
    already answers rather than raising, and runs the work on a thread of
    its own - but a submission is the one fact in this app that must be
    recorded whatever else is broken, so the call is wrapped as well. A
    failure here is a missing draft, which the Outreach card says out loud;
    an exception here would be a submission the person made and we did not
    keep.
    """
    try:
        from outreach import follow

        follow.start(job_id)
    except Exception as error:  # noqa: BLE001 - see the docstring
        logger.warning("follow-up drafts were not started for %s: %s", job_id, error)


@router.post("/{job_id}/submitted")
def mark_submitted(job_id: str, decision: Decision) -> dict:
    """Checkpoint 2. Only a human reaches this, after submitting by hand.

    The agent has no path to SUBMITTED: its terminal state is FILLED, and this
    endpoint is reachable only from the review page.

    This is also the **only** path that survives the tab being closed. The
    other three ways a submission is marked all read the tab the form was on
    - the page's own ping, `server/watch.py` over CDP, and `/screen`
    recognising a confirmation - so a person who presses Submit and closes
    the tab in the same second leaves this button as the whole mechanism.
    Which made the id collision fatal here rather than merely untidy: the
    page sent `e5b0`, two jobs carry it, and the 409 the person got back
    three times was about the other one. So the posting's own URL is
    accepted alongside the id and is what decides which job is meant
    (`_resolve_job`).
    """
    job, app_dir = _job_and_dir(job_id, decision.url or "")
    # From any live status, not only FILLED. FILLING was always accepted (the
    # human may finish the form while the agent is still poking at it; their
    # submission wins and the agent stops), and the same is true earlier:
    # someone who opened the posting and applied on the employer's site
    # before the fill ever ran is telling us a fact about the world, not
    # asking the pipeline for permission. Refusing it left the job sitting
    # in flight as a job still to do. Only a job already finished with is
    # refused, because there the button would be undoing a decision rather
    # than recording one.
    if job.status in (Status.SUBMITTED, Status.SKIPPED):
        raise HTTPException(409, f"job is already {job.status.value}")
    job_id = job.id
    killed = runner.stop_fill(job_id)
    note = decision.note or "submitted by hand"
    if killed:
        note += f" (fill {killed} still running, killed)"
    store.set_status(app_dir, Status.SUBMITTED, note)
    # By the row, not by the id: when the URL has just chosen between two
    # colliding rows, writing by the id would refuse (`queue.Ambiguous`) the
    # one write we are certain about.
    queue.update_row(job, status=Status.SUBMITTED)

    # One last look at the form before its tab goes, so the changes the
    # human may still pick to remember are the final ones. Never fails
    # the submission; nothing is learned by itself.
    corrections.capture(form_url(job, app_dir), app_dir)

    # The follow-up emails are prepared from here on (2026-10-30, asked
    # for): "Once the job is submitted, it should automatically go to a new
    # state where it is going through a follow... I do not have to do
    # anything." Preparation only - the people, the drafts, the resume
    # attached - and every one of them waits unsent for a click, because
    # delivery is the one thing in this app that is never automation.
    _prepare_follow_up(job.id)

    # Only this job's tab goes. The browser is the person's working set:
    # the Jobright list, the next job's form, their logins. Closing the
    # whole thing here read as a crash (2026-09-19).
    tab = "closed" if chrome.close_tab(_form_tab(job, app_dir)) else "not_open"
    return {"id": job_id, "status": Status.SUBMITTED.value, "tab": tab,
            "changes": corrections.changes(app_dir)}


@router.post("/{job_id}/submit")
def submit_now(job_id: str) -> dict:
    """Press Submit on this job's form, because the person asked for it here.

    The never-submit rule is about the agent, and it does not move: no model
    reaches this, no browser-use action is registered for it, `apply.py` does
    not import the presser, and the guard still refuses every submit control
    the agent sees. This is checkpoint 2 taken from the review page instead of
    from the form's own tab - one human click, confirmed in the page first,
    for the times the tailored form is already correct and walking over to the
    browser window is the only thing left.

    The form must be filled and its tab still open. The press waits for the
    page to become a confirmation: it does, the job is SUBMITTED with the
    confirmation's own words; it does not, the job is left exactly where it
    was and the reply says so, because a validation error is not a submission.
    The tab is never closed here - the confirmation is what the person wants
    to see.
    """
    from browser import press_submit

    job, app_dir = _job_and_dir(job_id)
    if job.status not in (Status.FILLED, Status.FILLING):
        raise HTTPException(409, f"job is {job.status.value}, not filled")
    target_id = _form_tab(job, app_dir)
    if not target_id:
        raise HTTPException(409, "the form's tab is not open; fill it again or submit by hand")

    # The last look at the form before it goes, so the changes the person may
    # still choose to remember are the ones they submitted.
    corrections.capture(form_url(job, app_dir), app_dir)
    try:
        result = press_submit.press_now(corrections.cdp_url(), target_id)
    except press_submit.NoSubmitControl as exc:
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - a dead socket is not a submission
        raise HTTPException(502, f"could not press submit: {exc}") from exc

    if not result.get("confirmed"):
        return {"id": job_id, "status": job.status.value, "submitted": False,
                "pressed": result.get("pressed", ""),
                "detail": "pressed Submit, but the form is still on the screen: "
                          "check the browser for what it is asking for"}
    runner.stop_fill(job_id)
    quote = result.get("text") or ""
    note = f"submitted from the review page (pressed “{result.get('pressed', '')}”)"
    if quote:
        note += f": “{quote[:120]}”"
    store.set_status(app_dir, Status.SUBMITTED, note)
    queue.update(job_id, status=Status.SUBMITTED)
    return {"id": job_id, "status": Status.SUBMITTED.value, "submitted": True,
            "pressed": result.get("pressed", ""), "quote": quote,
            "changes": corrections.changes(app_dir)}


class Pasted(BaseModel):
    reply: str = ""


@router.get("/{job_id}/handoff")
def handoff_prompt(job_id: str) -> dict:
    """The prompt to paste into Claude, for a job captured with Use Opus.

    Written by the pipeline when it stopped (`handoff.md`), served whole so
    the page's Copy button hands over exactly what the API would have been
    sent - rules, posting, stories, master resume, reply format.
    """
    _, app_dir = _job_and_dir(job_id)
    path = app_dir / "handoff.md"
    if not path.exists():
        raise HTTPException(404, "this job has no prompt waiting to be copied")
    text = path.read_text()
    return {"id": job_id, "prompt": text, "characters": len(text)}


@router.post("/{job_id}/handoff")
def handoff_reply(job_id: str, pasted: Pasted) -> dict:
    """The LaTeX the person brought back, held to every rule the API path is.

    Same checkers, same compile, same cover letter, same checkpoint 1 - the
    only difference is where the text came from. A rejection comes back as
    words for them to act on, with what they pasted still in the box, since
    here the person is the retry loop.
    """
    import pipeline as pipeline_module
    from tailor import byhand, tailor as tailor_module

    job, app_dir = _job_and_dir(job_id)
    if not (app_dir / "handoff.md").exists():
        raise HTTPException(409, "this job is not waiting for a resume")

    posting = pipeline_module.read_posting(app_dir, job)
    stories = profile.read_used(app_dir)
    base_tex = paths.BASE / "resume.tex"
    try:
        result = byhand.accept(pasted.reply, posting, base_tex.read_text(),
                               profile=tailor_module.load_profile(stories=stories))
    except tailor_module.TailorError as exc:
        raise HTTPException(422, str(exc)) from exc

    try:
        pipeline_module.finish(app_dir, job, posting, result, "", stories, base_tex,
                               pipeline_module.base_page_count(), None, False, False)
    except Exception as exc:  # noqa: BLE001 - a bad compile is the person's to see
        raise HTTPException(422, f"the resume did not compile: {exc}") from exc
    # The prompt has been answered; leaving it would offer the Copy button
    # over a finished application.
    (app_dir / "handoff.md").unlink(missing_ok=True)
    return {"id": job_id, "status": Status.AWAITING_REVIEW.value,
            "warnings": result.warnings, "folder": app_dir.name}


def form_url(job: Job, app_dir: Path) -> str:
    """Where this job's form actually is.

    The job's own URL, unless a Start now press recorded a different one: a
    YC listing, an employer's careers page or a job board link is a posting
    with the form one navigation away, and the tab the person reached is the
    one every later look at "the form" means - the tab lookup, the snapshot
    before a submission, the tab that closes when the job is finished with.
    The job's URL is not rewritten; it is the identity key.
    """
    from browser import fill as filler

    return filler.bound_form_url(app_dir) or job.url


def _form_tab(job: Job, app_dir: Path) -> str:
    """The tab holding this job's form: the id the fill recorded when it is
    still open, else the tab on the same form URL, else ""."""
    from browser import forms

    saved = corrections._read(app_dir / corrections.FILL_REPORT)
    return forms.find_target(corrections.cdp_url(), form_url(job, app_dir),
                             str(saved.get("target_id") or ""))


@router.post("/{job_id}/files")
def files_for_the_page(job_id: str) -> dict:
    """The tailored PDFs, base64, under their upload names.

    The capture script on the employer's page asks for these and shows them
    as draggable chips, so the person can drop a file on the form's own
    slot when the agent is slow or when they would rather do it by hand.
    POST because the page's bridge only posts; the body is unused. Base64
    because the bridge carries json, not bytes. Two PDFs is a few hundred
    KB, once per page.
    """
    import base64

    import apply as apply_script  # lazy: apply.py pulls in the browser stack

    _, app_dir = _job_and_dir(job_id)
    out = {}
    for key, source, stem in (("resume", "resume.pdf", apply_script.resume_filename()),
                              ("cover_letter", "cover_letter.pdf", apply_script.cover_letter_filename())):
        path = app_dir / source
        if path.exists():
            out[key] = {"name": f"{stem}.pdf", "type": "application/pdf",
                        "b64": base64.b64encode(path.read_bytes()).decode()}
    return out


# What the page may ask to be marked. The ref is the tag the chip put on the
# input it set, and the key says which document it was; the note and the
# filename are built here, so nothing the page sends is written onto the
# form's own page as text.
PUT_REFS = {"resume": ("put-resume", "the tailored resume (you put it there)"),
            "cover_letter": ("put-cover-letter", "the cover letter (you put it there)")}


class Put(BaseModel):
    url: str = ""
    key: str = "resume"


@router.post("/{job_id}/mark")
def mark_put(job_id: str, put: Put) -> dict:
    """Put the logo on a slot the page's "put" chip filled.

    The chip sets the form's own file input from the page, which is the same
    outcome as the fill's upload by the person's hand - and it was the one
    path with no logo in front of it, so a form filled that way looked
    untouched (2026-10-04, asked for). The mark is placed by `forms.mark_put`,
    which evaluates the same `MARK_FN` the fill uses, in the tab the chip was
    pressed in: one implementation, and the mark goes when the file changes
    like any other. Nothing on the form is touched here.
    """
    from browser import forms

    import apply as apply_script  # lazy: apply.py pulls in the browser stack

    job, app_dir = _job_and_dir(job_id)
    if put.key not in PUT_REFS:
        raise HTTPException(422, f"unknown document {put.key!r}")
    ref, note = PUT_REFS[put.key]
    source, stem = (("resume.pdf", apply_script.resume_filename()) if put.key == "resume"
                    else ("cover_letter.pdf", apply_script.cover_letter_filename()))
    if not (app_dir / source).exists():
        raise HTTPException(409, f"{source} is not in this job's folder")
    # The tab the chip was pressed in, which is not always the job's own URL:
    # a form opened by hand is exactly where "put" is wanted.
    target_id = forms.find_target(corrections.cdp_url(), put.url or job.url) or _form_tab(job, app_dir)
    if not target_id:
        raise HTTPException(409, "that tab is not open any more")
    try:
        marked = forms.mark_put_now(corrections.cdp_url(), target_id, ref, note, f"{stem}.pdf")
    except Exception as exc:  # noqa: BLE001 - the logo is advice, never the fill
        raise HTTPException(502, f"could not place the logo: {exc}") from exc
    return {"id": job_id, "marked": bool(marked), "ref": ref}


class Confirmation(BaseModel):
    url: str = ""
    quote: str = ""


@router.post("/{job_id}/submitted-seen")
def confirmation_seen(job_id: str, seen: Confirmation) -> dict:
    """The page in the browser showed a submission confirmation.

    Reported by the capture script watching the filled form's tab after the
    human pressed submit, and by `server/watch.py`, which reads the tab
    itself; the agent has no path here either, it never reaches the page
    after its own last action. Marks the job like the button does, but
    never closes the browser: the person is looking at it. A job that is
    not filled or filling is left alone, so a stray "thank you" on an
    unrelated page changes nothing.
    """
    job, app_dir = _job_and_dir(job_id)
    return mark_seen(job, app_dir, seen.url, seen.quote, "on the page")


def mark_seen(job: Job, app_dir: Path, url: str = "", quote: str = "", by: str = "") -> dict:
    """Submitted, because a confirmation was seen in the browser. Only a
    filled or filling job; the fill, if still running, stops; the last
    look at the form is what gets learned. Never closes anything."""
    if job.status not in (Status.FILLED, Status.FILLING):
        return {"id": job.id, "status": job.status.value, "marked": False}
    killed = runner.stop_fill(job.id)
    note = f"confirmation seen {by}".strip()
    if quote:
        note += f": “{quote[:120]}”"
    if url:
        note += f" at {url}"
    if killed:
        note += f" (fill {killed} still running, killed)"
    store.set_status(app_dir, Status.SUBMITTED, note)
    queue.update(job.id, status=Status.SUBMITTED)
    # The same preparation as the button's path: a submission recognised in
    # the browser is still a submission, and the follow-up should not depend
    # on which of the four paths noticed it.
    _prepare_follow_up(job.id)
    # The page has moved on to the confirmation; the last state the tab
    # reported before that is what the human can still pick from.
    return {"id": job.id, "status": Status.SUBMITTED.value, "marked": True,
            "changes": corrections.changes(app_dir)}


@router.post("/{job_id}/form-state")
def form_state(job_id: str) -> dict:
    """The tab says the form may have changed: look at it now. Sent by the
    capture script while the human works on a filled form and on its way
    out. Only a filled or filling job has a form worth looking at."""
    job, app_dir = _job_and_dir(job_id)
    if job.status not in (Status.FILLED, Status.FILLING):
        return {"id": job_id, "captured": False, "reason": job.status.value,
                "changes": corrections.changes(app_dir)}
    # The bound URL, not `job.url`: a form the person reached themselves is
    # on a URL our queue has never seen, and a snapshot aimed at the listing
    # would overwrite a good form state with a job description.
    return {"id": job_id, **corrections.capture(form_url(job, app_dir), app_dir),
            "changes": corrections.changes(app_dir)}


class Remember(BaseModel):
    labels: list[str]


@router.get("/{job_id}/changes")
def form_changes(job_id: str) -> dict:
    """What the human changed on this form since the fill: for the
    banner's and the page's pick list. Read, never decided."""
    _, app_dir = _job_and_dir(job_id)
    return {"id": job_id, "changes": corrections.changes(app_dir)}


@router.post("/{job_id}/remember")
def remember_changes(job_id: str, body: Remember) -> dict:
    """The human's pick of which changes to keep for every next form.
    The only way a correction gets on file."""
    _, app_dir = _job_and_dir(job_id)
    result = corrections.remember(app_dir, body.labels)
    return {"id": job_id, **result, "changes": corrections.changes(app_dir)}


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
        "status": (_resolve_job(job_id) or job).status.value,
    }


# ------------------------------------------------------------- Start now ----
#
# The fill stopped pressing Apply (2026-10-30, `browser/fill.py`). Pressing it
# was the autopilot trying to get itself from a posting to a form: up to three
# presses - posting, the system's chooser, "Apply Manually" - each of which can
# open a tab, and 22 runs this month still ended "no application form on this
# page". The person is already looking at the posting; their hand is both
# cheaper and more reliable than that guess.
#
# So the gate hands the job back exactly as it does today and this endpoint is
# the way forward: the person presses Apply themselves, the application's first
# page comes up, and **Start now** on the banner says so. It is not a second
# capture and not a second scheduler - it resolves the page to a job that is
# already in autopilot and starts that job's fill through the one gate every
# other start goes through (`runner.start_fill`), so one tab at a time still
# holds and nothing about never-submit moves: the fill still ends at the filled
# form.

start_router = APIRouter(tags=["review"])


class StartNow(BaseModel):
    """`url` is the page the person is standing on; `job_id` when the page
    already knows which job it is (the banner's match carries one)."""

    url: str = ""
    job_id: Optional[str] = None


# Every status a Start now press may begin a fill from. AWAITING_REVIEW is
# here and is the point of the endpoint: the job stopped at checkpoint 1
# because no form was on the screen, and the person pressing Start now with
# the form in front of them is the decision that was missing. QUEUED and
# TAILORING are not: there are no documents yet, and this endpoint starts
# fills, it does not start tailoring.
STARTABLE = (Status.AWAITING_REVIEW,) + REFILLABLE


def _start_reply(started: bool, state: str, job_id: str, note: str,
                 candidates: Optional[list[dict]] = None) -> dict:
    """The four keys the banner reads, plus `candidates` where a pick helps.

    `state` is one of:

    `started`   - a fill is now running for this job
    `synced`    - a fill was already running for it; nothing new was started
    `waiting`   - another job has the browser, or filling is switched off
    `not_ready` - the job has no documents to put on the form yet
    `submitted` / `rejected` - this job is finished with
    `no_job`    - no job in autopilot is certainly this page
    """
    return {"started": started, "state": state, "job_id": job_id, "note": note,
            "candidates": candidates or []}


# What the page offers to pick from when it cannot name the job itself. In
# flight means the fill is the next thing that has to happen to it.
# Still live: a job somebody could be standing in front of right now.
LIVE = (Status.AWAITING_REVIEW, Status.APPROVED, Status.FILLING, Status.FILLED)
# A job that stopped is also one somebody may be standing in front of -
# `failed` is exactly the state a person goes and finishes by hand - but
# there are 70 of those on this install against 4 live ones, so they come
# after the live jobs and only a few of them come at all.
STOPPED = (Status.FAILED,)
IN_FLIGHT = LIVE + STOPPED
# How many the picker offers. Measured on the live install: an unbounded
# list answered with 70 failed jobs, which is not a list anybody reads.
PICK_LIMIT = 8


def _in_flight() -> list[dict]:
    """The jobs a person standing on a form could plausibly mean, newest first.

    Needed because the identity ladder is right to answer "no job" here and
    the button still has to work. A YC listing
    (`ycombinator.com/companies/<org>/jobs/<id>`) is queued under YC's URL
    while its Apply link goes out to the company's own ATS, so by the time
    the person has signed in and reached the form they are on a Workday page
    with no `jr_id` and nothing in common with the URL on file. Nothing is
    guessed from that: the page asks which job this is, and the pick is sent
    back as `job_id`.
    """
    jobs = queue.all_jobs()

    def newest(rows):
        return sorted(rows, key=lambda job: job.added_at, reverse=True)

    # Live jobs first and all of them; then the most recent stopped ones to
    # fill the list up. Ordering by status before date is the whole point:
    # the one job that is filling right now must never be pushed off the
    # list by a month of failures that happen to be newer.
    rows = newest([job for job in jobs if job.status in LIVE])
    rows += newest([job for job in jobs if job.status in STOPPED])[:max(0, PICK_LIMIT - len(rows))]
    return [{"id": job.id, "title": job.title, "company": job.company,
             "status": job.status.value} for job in rows[:PICK_LIMIT]]


def _bind_tab(job: Job, app_dir: Path, url: str) -> bool:
    """Record that this job's form was reached at `url`, not at `job.url`.

    This is what makes the button work for a posting whose form is
    somewhere else entirely. The fill reads the last line of `form_url.txt`
    and starts in that tab (`browser/fill.bound_form_url`), through the tab
    finder it already uses, so there is no second way of locating a tab.
    Appended, never overwritten: the job's own URL stays the identity key
    the never-applied-twice ladder is built on, and the earlier landings are
    part of the record.
    """
    from browser import fill as filler

    if not url or seen.is_source_page(url):
        return False
    if identity_keys.canonical(url) == identity_keys.canonical(job.url):
        return False
    if filler.bound_form_url(app_dir) == url:
        return True          # already recorded; one line per new landing
    store.write_or_append(app_dir, filler.FORM_URL_FILE, f"{utcnow()} {url}\n")
    return True


@start_router.post("/start-now")
def start_now(body: StartNow) -> dict:
    """The person's hand where the automatic Apply press used to be.

    Never queues anything: a page that is not already a job answers
    `no_job`, because adding a posting is the Add button's decision and it
    screens first. Never starts a second fill either - `runner.fill_pid` and
    `runner.any_fill_running` are the existing one-tab-at-a-time gate and
    this asks them rather than working around them.
    """
    url = (body.url or "").strip()
    asked = (body.job_id or "").strip()
    job = _resolve_job(asked, url)
    if job is None:
        # The ladder is right to say no, and the button still has to work:
        # the person may have walked from a YC listing out to the employer's
        # own ATS, so the tab in front of them has nothing in common with
        # the URL on file. Nothing is guessed - the jobs that are waiting
        # for a fill go back with the answer, and the page asks.
        return _start_reply(False, "no_job", "",
                            "I cannot tell which job this page is: pick the one you are "
                            "applying for, or add this posting if it is new",
                            _in_flight())
    if job.status is Status.SUBMITTED:
        return _start_reply(False, "submitted", job.id,
                            "this application has already been submitted")
    if job.status is Status.SKIPPED:
        return _start_reply(False, "rejected", job.id,
                            "this job was rejected; use Try again on its page to reopen it")

    # Already working: say so rather than opening a second tab on one form.
    # Two fills on one job fight each other and undo each other's steps.
    running = runner.fill_pid(job.id)
    if running:
        return _start_reply(False, "synced", job.id,
                            f"a fill is already running for this job (pid {running})")

    if job.status not in STARTABLE or not job.app_dir:
        return _start_reply(False, "not_ready", job.id,
                            f"this job is {job.status.value}; there is nothing tailored "
                            f"to put on the form yet")
    app_dir = Path(job.app_dir)
    if not app_dir.exists() or not _tailored(app_dir):
        return _start_reply(False, "not_ready", job.id,
                            "this job has no tailored resume yet; open it in autopilot")

    other = runner.any_fill_running()
    if other and other != job.id:
        return _start_reply(False, "waiting", job.id,
                            "another job is being filled right now; this one goes next")

    # A fill is started as `apply.py <job id>`, so a job whose short id
    # names two rows cannot be aimed at: the URL told us which row the
    # person means, and the subprocess has only the id. Said plainly rather
    # than started and left to fail in a log nobody opens.
    if len(queue.rows_for(job.id)) > 1:
        return _start_reply(False, "not_ready", job.id,
                            f"two jobs share the id {job.id}, so a fill cannot be aimed "
                            f"at it; reject the duplicate and press again")

    # The tab the person is standing on is the form, whatever the job's own
    # URL says. Recorded before the fill starts, because the fill reads it.
    bound = _bind_tab(job, app_dir, url)

    # apply.py only touches an approved job, and a stuck FILLING must not be
    # able to wedge one, so the row goes to APPROVED first - the same step
    # `/fill` takes. The press itself is the approval: the person is looking
    # at the form.
    note = "start now, pressed on the page"
    if bound:
        note += f"; the form was reached at {url}"
    store.set_status(app_dir, Status.APPROVED, note)
    queue.update_row(job, status=Status.APPROVED)

    # `force`, because this is a human pressing on one job in front of them,
    # which is the same exception `/fill` makes - the hold exists to stop six
    # jobs opening six tabs by themselves, not to stop this one.
    pid = runner.start_fill(job.id, force=True)
    if not pid:
        return _start_reply(False, "waiting", job.id,
                            "approved, but the fill did not start: filling is switched "
                            "off (AUTOPILOT_AUTOFILL)")
    store.set_status(app_dir, Status.APPROVED, f"filling started from the page (pid {pid})")
    return _start_reply(True, "started", job.id,
                        "filling this form now - I will stop at the filled form for you"
                        + (" (on this tab)" if bound else ""))
