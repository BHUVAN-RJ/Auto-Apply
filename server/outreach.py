"""Human-reviewed recruiter outreach for one application.

Lookup and drafting are automation. Delivery is not: only the explicit send
route below can reach SMTP, and it refuses guessed addresses the person has
not acknowledged.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import mailing
import paths
from outreach import apollo, jobright, store
from outreach.models import Contact, Draft, now
from server import queue
from tailor import outreach as writer
from tailor import profile, tailor
from tailor.answers import Context

from .prompts import write_env

router = APIRouter(prefix="/review", tags=["outreach"])


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
    form = paths.BASE / "form.json"
    try:
        data = json.loads(form.read_text()) if form.exists() else {}
    except (OSError, ValueError):
        data = {}
    if isinstance(data, dict):
        full = str(data.get("full_name") or "").strip()
        if full:
            return full
        name = " ".join(str(data.get(key) or "").strip() for key in ("first_name", "last_name")).strip()
        if name:
            return name
    letter = app_dir / "cover_letter.md"
    if letter.exists():
        match = re.search(r"(?im)^(?:Sincerely|Best),\s*\n([^\n]+)", letter.read_text())
        if match:
            return match.group(1).strip()
    raise HTTPException(409, "your name is missing from Form details and the cover letter")


def _context(app_dir: Path) -> Context:
    applicant = paths.BASE / "applicant.md"
    return Context.from_app_dir(
        app_dir,
        profile=tailor.load_profile(stories=profile.read_used(app_dir)),
        applicant=applicant.read_text(errors="replace") if applicant.exists() else "",
    )


def _status(job_id: str) -> dict:
    outreach = store.get(job_id)
    return outreach.model_dump(mode="json") | {
        "apollo_configured": apollo.configured(),
        "mail_configured": mailing.configured(),
    }


@router.get("/{job_id}/outreach")
def status(job_id: str) -> dict:
    _job_and_dir(job_id)
    return _status(job_id)


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
    if draft.status != "draft":
        raise HTTPException(409, f"this message is already {draft.status}")
    if not request.confirmed:
        raise HTTPException(409, "confirm this recipient and message before sending")
    verified = draft.verified_by_user or request.verified_by_user
    if contact.verification != "verified" and not verified:
        raise HTTPException(409, "verify this guessed or manually entered address before sending")

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
