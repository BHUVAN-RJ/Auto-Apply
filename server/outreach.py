"""Human-reviewed recruiter outreach for one application.

Lookup and drafting are automation. Delivery is not: only the explicit send
route below can reach SMTP, and it refuses guessed addresses the person has
not acknowledged.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import mailing
import paths
from outreach import (apollo, follow, gmailthing, jobright, linkedinthing,
                      schedule as sendtime, store)
from outreach.models import Contact, Draft, now
from server import queue
from tailor import outreach as writer
from tailor.answers import Context

from .prompts import write_env

log = logging.getLogger("autopilot.outreach")

router = APIRouter(prefix="/review", tags=["outreach"])

# A sweep over applications already submitted. One worker, one job at a time:
# each job is up to three Apollo credits and three model calls, so ten of
# them started together is ten lookups and ten subprocesses on one laptop,
# and the credits are the person's money.
SWEEP_LIMIT = 25
_sweep_lock = threading.Lock()
_sweep: dict = {"running": False, "done": 0, "total": 0, "job": ""}


class MailLater(RuntimeError):
    """A scheduled email that must not go, with the reason worth recording."""


class DiscoverRequest(BaseModel):
    per_role: int = 3


class ManualContact(BaseModel):
    role: str
    name: str
    title: str = ""
    linkedin_url: str = ""
    email: str = ""


class ContactAction(BaseModel):
    contact_id: str


class DraftRequest(ContactAction):
    attach_resume: bool = True


class DraftBatch(BaseModel):
    contact_ids: list[str]
    attach_resume: bool = True


class DraftUpdate(BaseModel):
    subject: str
    body: str
    attach_resume: bool = True
    verified_by_user: bool = False


class SendRequest(BaseModel):
    confirmed: bool = False
    verified_by_user: bool = False
    # The person asked for their follow-ups to land at nine in the morning
    # rather than whenever they happened to review them, so the click
    # schedules by default. `now: true` is the page's "send it this minute"
    # and is still one explicit press.
    now: bool = False


class PrepareRequest(BaseModel):
    force: bool = False


class ApolloKey(BaseModel):
    key: str


class GmailConfig(BaseModel):
    user: str
    app_password: str


def _job_and_dir(job_id: str):
    job = queue.get(job_id)
    if job is None:
        raise HTTPException(404, f"no job {job_id}")
    if not job.app_dir:
        raise HTTPException(409, "the job has no tailored application folder yet")
    app_dir = Path(job.app_dir)
    if not app_dir.exists():
        raise HTTPException(409, "the tailored application folder is missing")
    return job, app_dir


def _candidate_name(app_dir: Path) -> str:
    name = follow.candidate_name(app_dir)
    if not name:
        raise HTTPException(409, "your name is missing from Form details and the cover letter")
    return name


def _context(app_dir: Path) -> Context:
    return follow.build_context(app_dir)


def _status(job_id: str) -> dict:
    outreach = store.get(job_id)
    return outreach.model_dump(mode="json") | {
        "apollo_configured": apollo.configured(),
        "mail_configured": mailing.configured(),
        # Which account the email leaves from. The card used to say only
        # whether mail was set up, so a send went out over whichever address
        # happened to be in .env and the person looked for it in the mailbox
        # they meant to use (2026-11-03).
        "mail_from": mailing.config()["sender"],
        # The account that logs in, which is what Gmail actually sends as.
        # Setting AUTOPILOT_MAIL_FROM to another address is not enough:
        # measured live (2026-11-03), Gmail silently rewrote a From of the
        # address the person wanted back to the account it had logged in
        # with, because the alias was not verified in that account's
        # settings. A card that showed only the wish would be lying.
        "mail_login": mailing.config()["user"],
        # What the click will do: schedule for this hour, or go at once.
        "send_hour": sendtime.hour(),
        "send_scheduled": sendtime.enabled(),
        "auto_prepare": follow.enabled(),
        "preparing": follow.running(job_id),
        # The person's own tools: the Gmail one writes from the address they
        # actually use, and the LinkedIn one owns the invite cap.
        "gmail_thing": gmailthing.enabled(),
        "linkedin_thing": linkedinthing.enabled(),
        "sending": going(job_id).get("step", ""),
    }


@router.get("/{job_id}/outreach")
def status(job_id: str) -> dict:
    _job_and_dir(job_id)
    return _status(job_id)


@router.post("/{job_id}/outreach/prepare")
def prepare(job_id: str, request: PrepareRequest) -> dict:
    """Run the automatic preparation by hand.

    The same work a submission starts by itself: find the people, write an
    email each, attach the tailored resume, send nothing. It answers at once
    with the state as it is now and the card polls for the rest.
    """
    _job_and_dir(job_id)
    follow.start(job_id, force=request.force)
    return _status(job_id)


class SweepRequest(BaseModel):
    job_ids: list[str] = []
    force: bool = False


def _sweep_state() -> dict:
    with _sweep_lock:
        return dict(_sweep)


def _run_sweep(job_ids: list[str], force: bool) -> None:
    try:
        for job_id in job_ids:
            with _sweep_lock:
                _sweep["job"] = job_id
            try:
                if follow.start(job_id, force=force):
                    follow.wait(job_id, timeout=600)
            except Exception as exc:              # noqa: BLE001 - one bad job is not the sweep
                log.warning("follow-up sweep skipped %s: %s", job_id, exc)
            with _sweep_lock:
                _sweep["done"] += 1
    finally:
        with _sweep_lock:
            _sweep["running"] = False
            _sweep["job"] = ""


@router.post("/followups/sweep")
def sweep(request: SweepRequest) -> dict:
    """Prepare follow-ups for applications that were submitted before this existed.

    The automatic path hangs off the submission itself, so every application
    sent before it was built has no drafts and never will by itself. This is
    how those are caught up, and it is deliberately the person's own press
    with the job ids named by the page: a selection policy of my own here
    would be a policy about spending their Apollo credits.
    """
    with _sweep_lock:
        if _sweep["running"]:
            return {"started": False} | dict(_sweep)
    wanted = []
    for job_id in request.job_ids[:SWEEP_LIMIT]:
        job = queue.get(job_id)
        if job is not None and job.app_dir and Path(job.app_dir).exists():
            wanted.append(job_id)
    if not wanted:
        raise HTTPException(409, "none of those jobs has a tailored application folder")
    with _sweep_lock:
        _sweep.update(running=True, done=0, total=len(wanted), job=wanted[0])
    threading.Thread(target=_run_sweep, args=(wanted, request.force),
                     name="autopilot-outreach-sweep", daemon=True).start()
    return {"started": True} | _sweep_state()


@router.get("/followups/sweep")
def sweep_status() -> dict:
    return _sweep_state()


@router.post("/{job_id}/outreach/apollo-key")
def save_apollo_key(job_id: str, request: ApolloKey) -> dict:
    _job_and_dir(job_id)
    key = request.key.strip()
    if len(key) < 20 or "\n" in key:
        raise HTTPException(400, "that does not look like an Apollo API key")
    write_env(paths.ENV_FILE, apollo.API_KEY, key)
    os.environ[apollo.API_KEY] = key
    return _status(job_id)


@router.post("/{job_id}/outreach/gmail")
def save_gmail(job_id: str, request: GmailConfig) -> dict:
    _job_and_dir(job_id)
    user = request.user.strip()
    password = request.app_password.strip()
    if "@" not in user or "\n" in user or len(password.replace(" ", "")) < 12:
        raise HTTPException(400, "enter your Gmail address and its App Password")
    write_env(paths.ENV_FILE, mailing.USER, user)
    write_env(paths.ENV_FILE, mailing.PASS, password)
    os.environ[mailing.USER] = user
    os.environ[mailing.PASS] = password
    return _status(job_id)


@router.post("/{job_id}/outreach/discover")
def discover(job_id: str, request: DiscoverRequest) -> dict:
    job, _ = _job_and_dir(job_id)
    if not job.company.strip():
        raise HTTPException(409, "the job has no company name; add a contact manually")
    try:
        contacts = apollo.discover(job.company, min(max(request.per_role, 1), 5))
    except apollo.ApolloError as exc:
        raise HTTPException(502, str(exc)) from None
    store.add_contacts(
        job_id, contacts,
        f"Apollo found {len(contacts)} contact(s); no email credits spent yet.",
    )
    return _status(job_id)


@router.post("/{job_id}/outreach/contact")
def add_contact(job_id: str, request: ManualContact) -> dict:
    job, _ = _job_and_dir(job_id)
    if request.role not in ("recruiter", "hiring_manager"):
        raise HTTPException(400, "role must be recruiter or hiring_manager")
    if not request.name.strip():
        raise HTTPException(400, "contact name is required")
    contact = Contact.create(
        role=request.role,
        name=request.name.strip(),
        title=request.title.strip(),
        company=job.company,
        linkedin_url=request.linkedin_url.strip(),
        email=request.email.strip(),
        source="manual",
        verification="manual",
        email_status="entered by you" if request.email.strip() else "",
    )
    store.add_contacts(job_id, [contact])
    return _status(job_id)


def _contact(job_id: str, contact_id: str) -> Contact:
    contact = store.get(job_id).contact(contact_id)
    if contact is None:
        raise HTTPException(404, "contact not found")
    return contact


@router.post("/{job_id}/outreach/apollo")
def enrich_apollo(job_id: str, request: ContactAction) -> dict:
    _job_and_dir(job_id)
    try:
        contact = apollo.enrich(_contact(job_id, request.contact_id))
    except apollo.ApolloError as exc:
        raise HTTPException(502, str(exc)) from None
    store.add_contacts(job_id, [contact], f"Apollo returned {contact.email_status or 'an email'}.")
    return _status(job_id)


@router.post("/{job_id}/outreach/jobright")
async def lookup_jobright(job_id: str, request: ContactAction) -> dict:
    _job_and_dir(job_id)
    try:
        contact = await jobright.lookup(_contact(job_id, request.contact_id))
    except jobright.JobrightError as exc:
        raise HTTPException(409, str(exc)) from None
    store.add_contacts(job_id, [contact], "Jobright returned an unverified email guess.")
    return _status(job_id)


def _existing_draft(job_id: str, contact_id: str) -> Draft | None:
    return next(
        (row for row in store.get(job_id).drafts
         if row.contact_id == contact_id and row.status in ("draft", "sending", "sent")),
        None,
    )


def _ensure_email(job_id: str, contact: Contact) -> Contact:
    if contact.email:
        return contact
    if not (contact.linkedin_url or contact.apollo_id):
        raise HTTPException(409, f"{contact.name} has no email yet")
    try:
        contact = apollo.enrich(contact)
    except apollo.ApolloError as exc:
        raise HTTPException(502, str(exc)) from None
    store.add_contacts(job_id, [contact], f"Apollo returned {contact.email_status or 'an email'}.")
    return contact


def _write_draft(job, app_dir, contact: Contact, attach_resume: bool) -> Draft:
    if not contact.email:
        raise HTTPException(409, "find or enter an email before drafting")
    try:
        return writer.draft(
            _context(app_dir), contact, job.title, job.company,
            _candidate_name(app_dir), attach_resume, app_dir.name,
        )
    except writer.OutreachError as exc:
        raise HTTPException(502, str(exc)) from None


@router.post("/{job_id}/outreach/draft")
def create_draft(job_id: str, request: DraftRequest) -> dict:
    job, app_dir = _job_and_dir(job_id)
    contact = _ensure_email(job_id, _contact(job_id, request.contact_id))
    store.put_draft(job_id, _write_draft(job, app_dir, contact, request.attach_resume))
    return _status(job_id)


@router.post("/{job_id}/outreach/drafts")
def create_drafts(job_id: str, request: DraftBatch) -> dict:
    """Write one carousel slide per selected contact. Never sends."""
    job, app_dir = _job_and_dir(job_id)
    ids = [row.strip() for row in request.contact_ids if row.strip()]
    if not ids:
        raise HTTPException(400, "select at least one person")
    if len(ids) > 12:
        raise HTTPException(400, "select at most 12 people at once")
    written, skipped, looked_up = 0, [], 0
    first_new = None
    for contact_id in ids:
        contact = _contact(job_id, contact_id)
        if _existing_draft(job_id, contact.id):
            skipped.append(f"{contact.name}: already has an email")
            continue
        had_email = bool(contact.email)
        try:
            contact = _ensure_email(job_id, contact)
        except HTTPException as exc:
            skipped.append(f"{contact.name}: {exc.detail}")
            continue
        if not had_email and contact.email:
            looked_up += 1
        try:
            draft = _write_draft(job, app_dir, contact, request.attach_resume)
        except HTTPException as exc:
            skipped.append(f"{contact.name}: {exc.detail}")
            continue
        store.put_draft(job_id, draft)
        if first_new is None:
            first_new = draft.id
        written += 1
    if written == 0 and skipped:
        raise HTTPException(409, "; ".join(skipped))
    note = f"Wrote {written} email(s)."
    if looked_up:
        note += f" Looked up {looked_up} address(es)."
    if skipped:
        note += " " + "; ".join(skipped)
    outreach = store.get(job_id)
    outreach.lookup_note = note
    if first_new:
        outreach.active_draft = first_new
    store.save(outreach)
    return _status(job_id)


@router.patch("/{job_id}/outreach/draft/{draft_id}")
def update_draft(job_id: str, draft_id: str, request: DraftUpdate) -> dict:
    _job_and_dir(job_id)
    outreach = store.get(job_id)
    draft = outreach.draft(draft_id)
    if draft is None:
        raise HTTPException(404, "draft not found")
    if draft.status != "draft":
        raise HTTPException(409, f"a {draft.status} draft cannot be edited")
    subject, body = request.subject.strip(), request.body.strip()
    if not subject or not body or "\n" in subject:
        raise HTTPException(400, "subject and body are required; subject must be one line")
    updated = draft.model_copy(update={
        "subject": subject,
        "body": body,
        "attach_resume": request.attach_resume,
        "verified_by_user": request.verified_by_user,
        "updated_at": now(),
    })
    store.put_draft(job_id, updated)
    return _status(job_id)


@router.get("/followups/queue")
def followups_queue() -> dict:
    """How the backfill is doing: what is being checked and how much is left."""
    from server import followqueue

    return followqueue.status()


class Switch(BaseModel):
    on: bool = True


@router.post("/followups/queue")
def followups_switch(request: Switch) -> dict:
    """Pause or resume the backfill. It spends allowance, so it can be stopped."""
    from server import followqueue, settings

    settings.save(outreach_backfill=bool(request.on))
    return followqueue.status()


@router.post("/{job_id}/outreach/done")
def outreach_done(job_id: str, request: Switch) -> dict:
    """Finished with this job's outreach: back under Applied.

    The flow the person described - submitted, then checking, then done -
    needs an end the person declares, because "every draft sent" is not the
    only honest ending: some of these are jobs they decide not to write to
    at all, and a group that can only be emptied by sending is a group that
    nags for ever.
    """
    _job_and_dir(job_id)
    outreach = store.get(job_id)
    outreach.done_at = now() if request.on else None
    store.save(outreach)
    return _status(job_id)


@router.post("/{job_id}/outreach/draft/{draft_id}/unschedule")
def unschedule(job_id: str, draft_id: str) -> dict:
    """Take an approved email back off the queue, before its hour."""
    _job_and_dir(job_id)
    draft = store.get(job_id).draft(draft_id)
    if draft is None:
        raise HTTPException(404, "draft not found")
    if draft.status != "scheduled":
        raise HTTPException(409, f"this message is {draft.status}, not scheduled")
    store.put_draft(job_id, sendtime.unschedule(draft))
    return _status(job_id)


def deliver_scheduled(job_id: str, draft_id: str) -> str:
    """Send one email whose hour has come. Called by `server/sendlater.py`.

    The gates are checked again here rather than trusted from the moment of
    the click: hours have passed, and the state on disk is what is true now.
    A draft that is no longer scheduled, or whose address lost its tick, is
    left exactly where it is.
    """
    job = queue.get(job_id)
    if job is None or not job.app_dir or not Path(job.app_dir).exists():
        raise MailLater(f"job {job_id} has no application folder")
    outreach = store.get(job_id)
    draft = outreach.draft(draft_id)
    if draft is None or draft.status != "scheduled":
        raise MailLater("the email is no longer scheduled")
    contact = outreach.contact(draft.contact_id)
    if contact is None or not contact.email:
        raise MailLater("the recipient has no email")
    if contact.verification != "verified" and not draft.verified_by_user:
        raise MailLater("the address lost its verification tick")
    _deliver(job_id, Path(job.app_dir), draft, contact, draft.verified_by_user)
    return draft.id



@router.post("/{job_id}/outreach/draft/{draft_id}/send")
def send(job_id: str, draft_id: str, request: SendRequest) -> dict:
    _, app_dir = _job_and_dir(job_id)
    outreach = store.get(job_id)
    draft = outreach.draft(draft_id)
    if draft is None:
        raise HTTPException(404, "draft not found")
    contact = outreach.contact(draft.contact_id)
    if contact is None or not contact.email:
        raise HTTPException(409, "the recipient has no email")
    # A scheduled email may be sent at once, which is the person taking it
    # off the queue and pressing properly; anything further along is done.
    if draft.status not in (("draft", "scheduled") if request.now else ("draft",)):
        raise HTTPException(409, f"this message is already {draft.status}")
    if not request.confirmed:
        raise HTTPException(409, "confirm this recipient and message before sending")
    verified = draft.verified_by_user or request.verified_by_user
    if contact.verification != "verified" and not verified:
        raise HTTPException(409, "verify this guessed or manually entered address before sending")

    # Their own Gmail is the first choice, and it has no hour to wait for:
    # the draft goes in front of them now and they press Send there, so
    # nothing is scheduled on this path.
    if gmailthing.enabled():
        return _hand_to_gmail(job_id, app_dir, draft, contact, verified)
    if sendtime.enabled() and not request.now:
        # Approved, and waiting for its hour. Nothing has been handed to SMTP.
        queued = sendtime.schedule(draft.model_copy(update={
            "verified_by_user": verified, "updated_at": now(),
        }))
        store.put_draft(job_id, queued)
        return _status(job_id)
    return _deliver(job_id, app_dir, draft, contact, verified)


class GoRequest(BaseModel):
    """One approval for a whole job: the emails and the invites."""

    confirmed: bool = False
    email: bool = True
    linkedin: bool = True


# One job at a time. Sending paces itself (their Gmail tool pauses between
# messages, their LinkedIn tool waits one to three minutes an invite), so a
# press is minutes of work and the page polls rather than waits.
_going: dict[str, dict] = {}
_going_lock = threading.Lock()


def going(job_id: str) -> dict:
    with _going_lock:
        return dict(_going.get(job_id) or {})


def _go(job_id: str, app_dir: Path, want_email: bool, want_linkedin: bool) -> None:
    """Draft, send, invite. Runs on a worker; every step records as it goes."""
    note = []
    try:
        if want_email and gmailthing.enabled():
            outreach = store.get(job_id)
            rows = []
            for draft in outreach.drafts:
                contact = outreach.contact(draft.contact_id)
                if contact is None or not contact.email:
                    continue
                if contact.verification != "verified" and not draft.verified_by_user:
                    note.append(f"{contact.name}: the guessed address still needs your tick")
                    continue
                if draft.status in ("draft", "scheduled"):
                    _hand_to_gmail(job_id, app_dir, draft, contact, draft.verified_by_user)
                current = store.get(job_id).draft(draft.id)
                if current is not None and current.status == "in_gmail" and current.gmail_draft_id:
                    rows.append((current, contact))
            if rows:
                with _going_lock:
                    _going[job_id] = {"step": f"sending {len(rows)} email(s)"}
                answer = gmailthing.send(
                    [{"draftId": row.gmail_draft_id, "to": contact.email}
                     for row, contact in rows])
                landed = {str(address).lower() for address in answer.get("recipients") or []}
                for row, contact in rows:
                    if contact.email.lower() in landed:
                        store.put_draft(job_id, row.model_copy(update={
                            "status": "sent", "sent_at": now(), "error": ""}))
                if answer.get("heldBack"):
                    note.append(f"{answer['heldBack']} email(s) held back by the daily cap")

        if want_linkedin and linkedinthing.enabled():
            with _going_lock:
                _going[job_id] = {"step": "sending connection requests"}
            waiting = [row for row in store.get(job_id).invites if row.status == "draft"]
            if waiting:
                send_invites(job_id, InviteRequest(confirmed=True,
                                                   invite_ids=[row.id for row in waiting]))
    except Exception as exc:  # noqa: BLE001 - the page has to be told
        note.append(str(exc))
        log.warning("outreach %s stopped: %s", job_id, exc)
    finally:
        state = store.get(job_id)
        if note:
            state.error = " ".join(note)
            store.save(state)
        with _going_lock:
            _going.pop(job_id, None)


@router.post("/{job_id}/outreach/go")
def go(job_id: str, request: GoRequest) -> dict:
    """The one press: send the emails and the connection requests.

    Asked for on 2026-11-05 - "linkedin and email draft and show in the job
    autopilot itself once i approve then you can start and then send
    completely". The approval is this press and there is no other way in:
    nothing here runs on a timer, on a verdict or on a submission.

    What it does not do is decide anything. Every address still needs its
    tick, every note is already written and on the card, and both tools keep
    their own caps and pacing.
    """
    _, app_dir = _job_and_dir(job_id)
    if not request.confirmed:
        raise HTTPException(409, "confirm before anything is sent")
    if going(job_id):
        raise HTTPException(409, "this job is already sending")
    with _going_lock:
        _going[job_id] = {"step": "starting"}
    threading.Thread(
        target=_go, args=(job_id, app_dir, request.email, request.linkedin),
        name=f"autopilot-outreach-go-{job_id}", daemon=True,
    ).start()
    return _status(job_id)


class InviteRequest(BaseModel):
    confirmed: bool = False
    # Which invites this press covers. Empty means every one still waiting,
    # which is what "approve once per job" asked for (2026-11-05).
    invite_ids: list[str] = []


@router.post("/{job_id}/outreach/linkedin/prepare")
def prepare_invites(job_id: str) -> dict:
    """Write one connection note per person who has a LinkedIn profile.

    Nothing is sent and nothing reaches LinkedIn: this is the writing step,
    so the person reads the exact words before anything is asked of anyone.
    """
    job, app_dir = _job_and_dir(job_id)
    if not linkedinthing.enabled():
        raise HTTPException(409, "the LinkedIn tool is not available")
    # The same writer the automatic preparation uses, so a press and a
    # submission cannot write the note two different ways. The press asks for
    # everybody on file; the automatic path asks for the people it chose.
    skipped: list[str] = []
    written = follow.write_invites(job_id, job, app_dir, skipped)
    state = store.get(job_id)
    if skipped:
        state.lookup_note = (state.lookup_note + " " + " ".join(skipped)).strip()
        store.save(state)
    log.info("outreach %s wrote %d connection notes", job_id, written)
    return _status(job_id)


@router.post("/{job_id}/outreach/linkedin/send")
def send_invites(job_id: str, request: InviteRequest) -> dict:
    """Really send the approved connection requests.

    The press is the gate, as everywhere else. Their own tool owns the rest:
    the 14-per-24-hours cap, one to three minutes between sends, no retry,
    and a stop at the first failure - so a batch that stops half way says so
    on every row rather than being tried again from here.
    """
    _job_and_dir(job_id)
    if not request.confirmed:
        raise HTTPException(409, "confirm these notes before anything is sent")
    if not linkedinthing.enabled():
        raise HTTPException(409, "the LinkedIn tool is not available")
    outreach = store.get(job_id)
    waiting = [row for row in outreach.invites if row.status == "draft"
               and (not request.invite_ids or row.id in request.invite_ids)]
    if not waiting:
        raise HTTPException(409, "there is nothing waiting to be sent")
    for row in waiting:
        store.put_invite(job_id, row.model_copy(update={"status": "sending", "error": ""}))
    try:
        answer = linkedinthing.invite(
            [{"profileUrl": row.profile_url, "note": row.note} for row in waiting])
    except linkedinthing.LinkedInThingError as exc:
        for row in waiting:
            store.put_invite(job_id, row.model_copy(update={
                "status": "draft", "error": str(exc)}))
        raise HTTPException(502, str(exc)) from None

    # Their tool answers per person, and its words are the ones recorded:
    # "sent" here means its tracker wrote a send, never that we assumed one.
    by_url = {str(item.get("profileUrl") or ""): item for item in answer.get("items") or []}
    for row in answer.get("skipped") or []:
        by_url.setdefault(str(row.get("profileUrl") or ""), {
            "status": f"skipped: {row.get('reason', 'skipped')}"})
    for row in waiting:
        said = by_url.get(row.profile_url) or {}
        state = str(said.get("status") or "")
        if state == "sent":
            store.put_invite(job_id, row.model_copy(update={
                "status": "sent", "sent_at": now(), "error": ""}))
        elif state.startswith("skipped"):
            store.put_invite(job_id, row.model_copy(update={
                "status": "skipped", "reason": state.split(": ", 1)[-1]}))
        else:
            store.put_invite(job_id, row.model_copy(update={
                "status": "failed" if state == "failed" else "draft",
                "error": str(said.get("error") or answer.get("status") or
                             "the tool did not say what happened")}))
    return _status(job_id)


def _hand_to_gmail(job_id: str, app_dir: Path, draft: Draft, contact: Contact,
                   verified: bool) -> dict:
    """Write this approved email into the person's own Gmail, as a draft.

    Their own tool does it (`outreach/gmailthing.py` says why), so it leaves
    from the address they actually write from rather than from whichever
    mailbox autopilot can log into - the defect this path exists to fix.
    Nothing is sent here: the draft is in Gmail with the tailored resume on
    it, and sending it is a separate, explicit step.
    """
    attachment = None
    if draft.attach_resume:
        import apply as apply_script
        attachment = apply_script.upload_copy(app_dir)
        if attachment is None:
            raise HTTPException(409, "the tailored resume PDF is missing")
    working = draft.model_copy(update={
        "status": "sending", "verified_by_user": verified,
        "error": "", "updated_at": now(),
    })
    store.put_draft(job_id, working)
    try:
        made = gmailthing.draft(contact.email, draft.subject, draft.body, attachment)
    except gmailthing.GmailThingError as exc:
        store.put_draft(job_id, working.model_copy(update={
            "status": "draft", "error": str(exc), "updated_at": now()}))
        raise HTTPException(502, str(exc)) from None

    landed = working.model_copy(update={
        "status": "in_gmail", "updated_at": now(),
        "gmail_draft_id": made["draft_id"], "gmail_account": made["account"],
        # Whether the resume is on it is read back off Gmail by their tool,
        # so the card states a fact rather than repeating our own intention.
        "error": "" if (made["attached"] or not draft.attach_resume)
                 else "the draft is in Gmail without the resume on it",
    })
    try:
        store.immutable_sent_record(app_dir, contact, landed, attachment)
    except Exception as exc:  # noqa: BLE001 - the draft exists; record that
        landed.error = f"drafted, but the audit record failed: {exc}"
    store.put_draft(job_id, landed)
    return _status(job_id)


def _deliver(job_id: str, app_dir: Path, draft: Draft, contact: Contact,
             verified: bool) -> dict:
    """Hand one approved email to SMTP. The only path to the outside world.

    Reached from the route above when the person asked for it to go now, and
    from `server/sendlater.py` when a scheduled hour comes round. Both have
    already checked the gates; this re-reads nothing and decides nothing
    except how to build the message.
    """
    attachment = None
    if draft.attach_resume:
        import apply as apply_script
        attachment = apply_script.upload_copy(app_dir)
        if attachment is None:
            raise HTTPException(409, "the tailored resume PDF is missing")

    try:
        msg = mailing.message(
            contact.email, draft.subject, draft.body,
            attachments=[attachment] if attachment else [],
        )
    except mailing.MailError as exc:
        raise HTTPException(409, str(exc)) from None
    draft = draft.model_copy(update={
        "status": "sending", "message_id": str(msg["Message-ID"]),
        "verified_by_user": verified, "error": "", "updated_at": now(),
    })
    store.put_draft(job_id, draft)
    try:
        mailing.send_message(msg)
    except mailing.MailError as exc:
        failed = draft.model_copy(update={
            "status": "failed", "error": str(exc), "updated_at": now(),
        })
        store.put_draft(job_id, failed)
        raise HTTPException(502, str(exc)) from None

    sent = draft.model_copy(update={
        "status": "sent", "sent_at": now(), "updated_at": now(), "error": "",
    })
    try:
        store.immutable_sent_record(app_dir, contact, sent, attachment)
    except Exception as exc:  # message went out; record that truth even if archive failed
        sent.error = f"sent, but the immutable audit record failed: {exc}"
    store.put_draft(job_id, sent)
    return _status(job_id)
