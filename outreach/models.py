"""Data kept for outreach independently of an application's lifecycle."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, Field


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def stable_id(*parts: str) -> str:
    value = "\0".join(str(part or "").strip().lower() for part in parts)
    return hashlib.sha256(value.encode()).hexdigest()[:12]


class Contact(BaseModel):
    id: str
    role: Literal["recruiter", "hiring_manager"]
    name: str
    title: str = ""
    company: str = ""
    linkedin_url: str = ""
    apollo_id: str = ""
    email: str = ""
    # "claude" is the free first step of the ladder: the public web read by
    # the Claude Code subscription, which costs an allowance and not a credit.
    source: Literal["apollo", "jobright", "claude", "manual"] = "manual"
    verification: Literal["verified", "guessed", "manual"] = "manual"
    email_status: str = ""
    created_at: str = Field(default_factory=now)

    @classmethod
    def create(cls, role: str, name: str, **values) -> "Contact":
        identity = values.get("linkedin_url") or values.get("apollo_id") or values.get("email") or name
        return cls(id=stable_id(role, identity), role=role, name=name, **values)


class Draft(BaseModel):
    id: str
    contact_id: str
    subject: str
    body: str
    attach_resume: bool = True
    resume_folder: str = ""
    # The filename the resume will go out as, filled in once the named copy
    # exists, so "resume attached" on the card is a fact and not a promise.
    attachment_name: str = ""
    # "scheduled" is an approved email waiting for its hour: the person has
    # clicked, and the click is what the standing rule is about, but they
    # asked for the mail to land at a civil time rather than whenever they
    # happened to be reviewing (2026-11-03).
    # "in_gmail" is an approved email that now exists as a draft in the
    # person's own Gmail, written from their own address with the resume on
    # it (2026-11-05). It is the end of autopilot's part: the Send button is
    # Gmail's, where the mail actually lives.
    status: Literal["draft", "scheduled", "sending", "sent", "in_gmail",
                    "failed"] = "draft"
    # When it goes, local time, ISO with an offset. Only set while scheduled.
    send_at: Optional[str] = None
    verified_by_user: bool = False
    model: str = ""
    created_at: str = Field(default_factory=now)
    updated_at: str = Field(default_factory=now)
    sent_at: Optional[str] = None
    message_id: str = ""
    # The Gmail draft this became, and the account it was written in: the id
    # is what the Gmail connector's `delete_draft` takes, and the account is
    # the whole reason this path exists.
    gmail_draft_id: str = ""
    gmail_account: str = ""
    error: str = ""

    @classmethod
    def create(cls, contact_id: str, subject: str, body: str, **values) -> "Draft":
        # A draft id has to be unique, not reproducible: `stable_id` over the
        # contact and the clock gives two drafts written for one person in the
        # same second the same id, and `store.put_draft` then replaces the
        # first with the second rather than keeping both.
        return cls(
            id=uuid4().hex[:12],
            contact_id=contact_id,
            subject=subject,
            body=body,
            **values,
        )


class Invite(BaseModel):
    """One LinkedIn connection request, written and waiting for a press.

    The note is the whole message: LinkedIn shows it word for word and their
    own guide (`docs/connection-note-guide.md` in the LinkedIn tool) says how
    it is written. "sent" here means their tracker recorded a send, never
    that we assumed one.
    """

    id: str
    contact_id: str
    profile_url: str
    note: str
    status: Literal["draft", "sending", "sent", "skipped", "failed"] = "draft"
    reason: str = ""          # why it was skipped, in their tool's words
    model: str = ""
    created_at: str = Field(default_factory=now)
    updated_at: str = Field(default_factory=now)
    sent_at: Optional[str] = None
    error: str = ""

    @classmethod
    def create(cls, contact_id: str, profile_url: str, note: str, **values) -> "Invite":
        return cls(id=uuid4().hex[:12], contact_id=contact_id,
                   profile_url=profile_url, note=note, **values)


class Outreach(BaseModel):
    job_id: str
    contacts: list[Contact] = Field(default_factory=list)
    drafts: list[Draft] = Field(default_factory=list)
    # The other half of reaching a company: a connection request to the same
    # people, carrying a note rather than a resume (2026-11-05).
    invites: list[Invite] = Field(default_factory=list)
    active_draft: str = ""
    lookup_note: str = ""
    error: str = ""
    # Automatic follow-up preparation, which starts by itself once the person
    # has submitted the application. "running" is a claim on the work, so a
    # second trigger for the same job does nothing rather than looking the
    # same people up twice and spending a second Apollo credit on each.
    prepare_status: Literal["", "running", "ready", "failed"] = ""
    prepared_at: Optional[str] = None
    # The person is finished with this job's outreach - sent what they wanted
    # to send, or decided to send nothing. It leaves the "emails to send"
    # group and the application goes back under Applied, which is the flow
    # they described: submitted, then checking, then done.
    done_at: Optional[str] = None
    updated_at: str = Field(default_factory=now)

    def contact(self, contact_id: str) -> Optional[Contact]:
        return next((row for row in self.contacts if row.id == contact_id), None)

    def draft(self, draft_id: str) -> Optional[Draft]:
        return next((row for row in self.drafts if row.id == draft_id), None)

    def invite(self, invite_id: str) -> Optional[Invite]:
        return next((row for row in self.invites if row.id == invite_id), None)
