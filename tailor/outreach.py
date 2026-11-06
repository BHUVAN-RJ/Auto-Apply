"""Grounded recruiter outreach, prepared for a human to edit and send."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from outreach.models import Contact, Draft

from . import cover, llm, prompts
from .answers import Context

RULES = Path(__file__).resolve().parent / "outreach_rules.md"
REPLY_FORMAT = """
## Reply format

Reply with exactly two fenced blocks and nothing else:

```subject
<subject line>
```

```email
<plain-text email>
```
"""
ATTEMPTS = 3
MIN_WORDS = 60
MAX_WORDS = 160


class OutreachError(RuntimeError):
    pass


@dataclass
class Written:
    subject: str
    body: str
    model: str
    attempts: int


def system_prompt() -> str:
    return prompts.text("outreach") + "\n" + prompts.text("outreach.format")


def _block(reply: str, name: str) -> str:
    match = re.search(rf"```{name}\s*\n(.*?)```", reply, re.S | re.I)
    return match.group(1).strip() if match else ""


def problems(subject: str, body: str) -> list[str]:
    found = []
    subject_words = len(subject.split())
    words = len(body.split())
    if not 4 <= subject_words <= 9:
        found.append(f"the subject was {subject_words} words; use 4 to 9")
    if not MIN_WORDS <= words <= MAX_WORDS:
        found.append(f"the email was {words} words; use {MIN_WORDS} to {MAX_WORDS}")
    if not re.search(r"^Best,\s*$", body, re.I | re.M):
        found.append('the email did not close with "Best," and the candidate name')
    if re.search(r"^\s*[-*#>]", body, re.M) or "**" in body:
        found.append("the email used markdown or a list")
    if cover.has_dash(body):
        found.append("the email used a dash as punctuation")
    return found


def write(context: Context, contact: Contact, job_title: str, company: str,
          candidate_name: str, attach_resume: bool = True,
          model: Optional[str] = None) -> Written:
    if not context.posting or not context.resume_tex:
        raise OutreachError("the posting and tailored resume are required")
    if not contact.name:
        raise OutreachError("the contact needs a name")
    model = model or llm.outreach_model()
    user = context.as_message() + (
        "\n\n## Recipient\n\n"
        f"Name: {contact.name}\nTitle: {contact.title or 'unknown'}\n"
        f"Role in outreach: {contact.role.replace('_', ' ')}\n"
        f"Company: {company or contact.company}\n"
        f"Job: {job_title}\n"
        f"Candidate name: {candidate_name}\n"
        f"Tailored resume attached: {'yes' if attach_resume else 'no'}"
    )
    last_subject = last_body = ""
    last_problems = []
    for attempt in range(1, ATTEMPTS + 1):
        reply = llm.complete(
            system_prompt(), user, model=model, temperature=0.4, max_tokens=1200,
        )
        subject = " ".join(_block(reply, "subject").split())
        body = _block(reply, "email").strip()
        last_subject, last_body = subject, body
        last_problems = problems(subject, body)
        if not last_problems:
            return Written(subject, body, model, attempt)
        user += (
            "\n\n## Previous reply rejected\n\n"
            + "; ".join(last_problems)
            + ". Rewrite both blocks and keep every grounded fact."
        )
    raise OutreachError(
        f"outreach draft broke the rules on {ATTEMPTS} attempts: "
        + "; ".join(last_problems)
        + f" (last subject: {last_subject!r}, body words: {len(last_body.split())})"
    )


def draft(context: Context, contact: Contact, job_title: str, company: str,
          candidate_name: str, attach_resume: bool = True,
          resume_folder: str = "", model: Optional[str] = None) -> Draft:
    written = write(
        context, contact, job_title, company, candidate_name,
        attach_resume=attach_resume, model=model,
    )
    return Draft.create(
        contact_id=contact.id,
        subject=written.subject,
        body=written.body,
        attach_resume=attach_resume,
        resume_folder=resume_folder,
        model=written.model,
    )


# --- LinkedIn connection notes --------------------------------------------

NOTE_LIMIT = 300        # LinkedIn's own hard limit
NOTE_AIM = 200          # what their guide asks for, so every reading fits
NOTE_ATTEMPTS = 3
NOTE_SYSTEM = """You write one LinkedIn connection note, and nothing else.

A note is at most 200 characters, addressed to one person by first name, and
it says who the candidate is, what they are after, and one reason this person
was chosen. It is the whole message the person sees beside the request.

The guide below is the candidate's own, written from what they have had
accepted. Follow it exactly, including what it refuses.

Reply with the note itself on one line. No quotes, no sign-off, no subject,
no explanation."""


def note_problems(note: str) -> list[str]:
    found = []
    if len(note) > NOTE_AIM:
        found.append(f"the note was {len(note)} characters; keep it under {NOTE_AIM}")
    if not note.strip():
        found.append("the note was empty")
    if "\n" in note.strip():
        found.append("the note ran to more than one line")
    if cover.has_dash(note):
        found.append("the note used a dash as punctuation")
    for banned in ("pick your brain", "be my mentor", "I came across your profile",
                   "any open roles", "I'd love to connect"):
        if banned.lower() in note.lower():
            found.append(f"the note used a phrase the guide refuses: {banned!r}")
    return found


def note(contact: Contact, job_title: str, company: str, candidate: str,
         guide: str, applied: bool = True, model: Optional[str] = None) -> tuple[str, str]:
    """One connection note for one person. Returns the note and the model.

    The guide is passed in rather than read here, because it belongs to the
    person's own LinkedIn tool and that tool is where they edit it.
    """
    if not contact.name:
        raise OutreachError("the contact needs a name")
    model = model or llm.outreach_model()
    user = (
        f"## The guide\n\n{guide.strip()}\n\n"
        "## The candidate\n\n"
        f"Name: {candidate}\n"
        f"They have just applied for: {job_title} at {company}\n"
        if applied else
        f"## The guide\n\n{guide.strip()}\n\n## The candidate\n\nName: {candidate}\n"
    )
    user += (
        "\n## The person\n\n"
        f"Name: {contact.name}\n"
        f"Title: {contact.title or 'unknown'}\n"
        f"They are the: {contact.role.replace('_', ' ')}\n"
        f"Company: {company or contact.company}\n"
    )
    problems_found: list[str] = []
    for _ in range(NOTE_ATTEMPTS):
        reply = llm.complete(NOTE_SYSTEM, user, model=model, temperature=0.5, max_tokens=300)
        written = " ".join(reply.strip().strip('"').split())
        problems_found = note_problems(written)
        if not problems_found:
            return written, model
        user += ("\n\n## Previous note rejected\n\n" + "; ".join(problems_found)
                 + ". Write it again, shorter, keeping the person's first name.")
    raise OutreachError("the connection note broke the guide: " + "; ".join(problems_found))
