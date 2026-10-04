"""Data kept for outreach independently of an application's lifecycle."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Literal, Optional

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
    source: Literal["apollo", "jobright", "manual"] = "manual"
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
    status: Literal["draft", "sending", "sent", "failed"] = "draft"
    verified_by_user: bool = False
    model: str = ""
    created_at: str = Field(default_factory=now)
    updated_at: str = Field(default_factory=now)
    sent_at: Optional[str] = None
    message_id: str = ""
    error: str = ""

    @classmethod
    def create(cls, contact_id: str, subject: str, body: str, **values) -> "Draft":
        return cls(
            id=stable_id(contact_id, now()),
            contact_id=contact_id,
            subject=subject,
            body=body,
            **values,
        )


class Outreach(BaseModel):
    job_id: str
    contacts: list[Contact] = Field(default_factory=list)
    drafts: list[Draft] = Field(default_factory=list)
    active_draft: str = ""
    lookup_note: str = ""
    error: str = ""
    updated_at: str = Field(default_factory=now)

    def contact(self, contact_id: str) -> Optional[Contact]:
        return next((row for row in self.contacts if row.id == contact_id), None)

    def draft(self, draft_id: str) -> Optional[Draft]:
        return next((row for row in self.drafts if row.id == draft_id), None)
