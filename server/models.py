"""Shared data models for the queue and application state."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slugify(value: str, max_length: int = 40) -> str:
    """Lowercase, hyphenated, filesystem-safe fragment of a title."""
    value = re.sub(r"[^\w\s-]", "", value.lower())
    value = re.sub(r"[\s_-]+", "-", value).strip("-")
    return value[:max_length].strip("-") or "unknown"


def url_hash(url: str) -> str:
    """Four hex characters of the URL, enough to disambiguate same-day jobs."""
    return hashlib.sha256(url.encode()).hexdigest()[:4]


class RejectReason(str, Enum):
    """Why an application was dropped.

    Structured rather than free text so the reasons can be counted later; a
    note always travels alongside for the specifics.
    """

    POOR_FIT = "poor_fit"
    LOCATION = "location"
    SENIORITY = "seniority"
    COMPENSATION = "compensation"
    RESUME_WRONG = "resume_wrong"
    CHANGED_MY_MIND = "changed_my_mind"
    OTHER = "other"


REJECT_LABELS = {
    RejectReason.POOR_FIT: "Not a good fit",
    RejectReason.LOCATION: "Location or work authorisation",
    RejectReason.SENIORITY: "Wrong seniority",
    RejectReason.COMPENSATION: "Compensation",
    RejectReason.RESUME_WRONG: "Tailoring was wrong",
    RejectReason.CHANGED_MY_MIND: "Changed my mind",
    RejectReason.OTHER: "Other",
}


class Status(str, Enum):
    """Lifecycle of a single job through the pipeline.

    The agent may set every value except SUBMITTED, which only a human sets
    after submitting the form by hand.
    """

    QUEUED = "queued"
    TAILORING = "tailoring"
    AWAITING_REVIEW = "awaiting_review"  # checkpoint 1
    APPROVED = "approved"
    FILLING = "filling"
    FILLED = "filled"  # checkpoint 2, terminal for the agent
    SUBMITTED = "submitted"  # human-set only
    SKIPPED = "skipped"
    FAILED = "failed"


class Job(BaseModel):
    """One captured posting. Created by the capture extension."""

    url: str
    title: str = ""
    source: str = ""
    company: str = ""
    added_at: str = Field(default_factory=utcnow)
    status: Status = Status.QUEUED
    app_dir: Optional[str] = None
    error: Optional[str] = None
    reject_reason: Optional[RejectReason] = None
    reject_note: Optional[str] = None
    # Per-job answer to "pass checkpoint 1 without me?": the banner's
    # auto-approve box at capture time. None = the global auto_fill switch.
    auto_fill: Optional[bool] = None

    @property
    def id(self) -> str:
        return url_hash(self.url)

    def folder_name(self, version: int = 1) -> str:
        """`2026-09-10_stripe_backend-engineer_a3f1`, with `_v2` on re-runs."""
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        company = slugify(self.company or "unknown-company", 24)
        role = slugify(self.title or "unknown-role", 32)
        name = f"{date}_{company}_{role}_{self.id}"
        return name if version == 1 else f"{name}_v{version}"
