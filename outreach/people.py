"""Who to write to, found on the public web by the Claude Code subscription.

The paid lookup used to be the only way in: Apollo's people search to learn
who the recruiters are, then Apollo's enrichment to learn an address, and the
enrichment is what spends a credit per person. The person's own words for
what they wanted instead: "1st it links to my claude and then it tries to
find people and then after that fails it then asks appolo - just to save
credits".

So this is the first step of the ladder and Apollo is the last. It costs the
subscription's allowance rather than money, and `tailor/claudecode.py` is
where that call lives.

Two things it is careful about, because an outreach email goes out over the
person's own name to a named human:

- **Nothing here is verified.** An address found on the web is a claim about
  the world, and the model is asked to cite the page it read it on. It is
  recorded as `verification="guessed"` even with a citation, so the send
  route still demands the person's own "I verified this address" tick. Only
  Apollo's enrichment is `verified`, and the card's per-contact Apollo button
  is one click away when certainty is worth a credit.
- **A person with no evidence is not invented.** A reply naming somebody
  without a LinkedIn URL or a title is kept only as a name to look up, and a
  reply that parses to nothing is a miss, said as one, not an error.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Optional

from .models import Contact

log = logging.getLogger("autopilot.outreach")

ENV = "AUTOPILOT_OUTREACH_CLAUDE"      # 0 goes straight to Apollo, as before
ROLES = ("recruiter", "hiring_manager")


class PeopleError(RuntimeError):
    """A reason worth showing the person on the card."""


def enabled() -> bool:
    return os.environ.get(ENV, "1").strip() not in ("0", "false", "no")


def available() -> bool:
    from tailor import claudecode

    return enabled() and claudecode.available()


FIND_PROMPT = """\
Find the people at one company who would read a job application.

Company: {company}
Role applied for: {title}

Use web search. Find up to {per_role} technical recruiter(s) or talent
partner(s), and up to {per_role} engineering hiring manager(s), director(s)
or VP(s) who could own this role. Prefer people whose current employer is
this company on a page you actually read.

Reply with a JSON array and nothing else. No prose, no code fence.

[{{"role": "recruiter" or "hiring_manager",
   "name": "full name",
   "title": "their current title",
   "linkedin_url": "their LinkedIn profile URL, or \\"\\"",
   "email": "their work email ONLY if a page you read literally shows it, else \\"\\"",
   "evidence": "the URL of the page that told you this"}}]

Rules you must not break:
- The person must work at this company **now**, on a page you read. Somebody
  with the right job title at another company is a wrong answer, not a near
  one: an email about this opening would go to a stranger.
- Never guess, derive or pattern-match an email address. An address goes in
  only when a page shows that exact address for that exact person.
- Never invent a person. An empty array is the right answer when the web
  does not say.
- No generic inbox (careers@, jobs@, info@, hr@): this is for a named human.
"""

ADDRESS_PROMPT = """\
Find one person's work email address, or say you could not.

Name: {name}
Title: {title}
Company: {company}
LinkedIn: {linkedin}

Use web search. Reply with a JSON object and nothing else:

{{"email": "the address, or \\"\\"", "evidence": "the URL of the page showing it"}}

Never guess, derive or pattern-match the address, and never answer with a
generic inbox. An empty string is the right answer when no page shows it.
"""

GENERIC = re.compile(
    r"^(careers?|jobs?|info|hr|hello|contact|recruit(ing|ment)?|talent|support|"
    r"admin|team|people|hiring|apply|noreply|no-reply)@", re.I)
EMAIL = re.compile(r"^[^@\s]+@[^@\s.]+\.[^@\s]+$")


def _ask(prompt: str) -> str:
    from tailor import claudecode

    if not available():
        raise PeopleError("the Claude Code CLI is not available")
    text, usage = claudecode.research(prompt)
    log.info("outreach research: %ss, reported %s", usage.get("seconds"),
             usage.get("reported_cost"))
    return text


def parse(reply: str) -> list[dict]:
    """The JSON array out of a reply, however it was fenced.

    A model asked for bare JSON mostly sends bare JSON, and a fence or a
    sentence in front of it is not a reason to lose the answer.
    """
    text = (reply or "").strip()
    fence = re.search(r"```(?:json)?\s*\n(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    if not text.startswith(("[", "{")):
        match = re.search(r"\[.*\]", text, re.S)
        text = match.group(0) if match else text
    try:
        data = json.loads(text)
    except ValueError:
        return []
    if isinstance(data, dict):
        data = [data]
    return [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []


def usable_email(value: str) -> str:
    """"" for anything that is not one named human's own address."""
    email = str(value or "").strip().strip("<>").lower()
    if not EMAIL.match(email) or GENERIC.match(email):
        return ""
    return email


def _contact(row: dict, company: str) -> Optional[Contact]:
    role = str(row.get("role") or "").strip().lower().replace(" ", "_")
    name = " ".join(str(row.get("name") or "").split())
    if role not in ROLES or not name or len(name) < 3:
        return None
    linkedin = str(row.get("linkedin_url") or "").strip()
    if linkedin and "linkedin.com/in/" not in linkedin.lower():
        linkedin = ""
    title = " ".join(str(row.get("title") or "").split())
    if not linkedin and not title:
        return None                      # a bare name is not a person found
    email = usable_email(row.get("email", ""))
    evidence = str(row.get("evidence") or "").strip()
    return Contact.create(
        role, name, title=title, company=company, linkedin_url=linkedin,
        email=email, source="claude",
        # Cited or not, this is not a verified address: the send route still
        # asks the person to tick it.
        verification="guessed" if email else "manual",
        email_status=(f"found by Claude on {evidence}"[:200] if email else ""),
    )


def find(company: str, job_title: str = "", per_role: int = 2) -> list[Contact]:
    """The people at one company, found on the public web. [] is a miss."""
    if not company.strip():
        raise PeopleError("the job has no company name to search for")
    reply = _ask(FIND_PROMPT.format(
        company=company.strip(), title=job_title.strip() or "an engineering role",
        per_role=max(1, per_role),
    ))
    found: dict[str, Contact] = {}
    for row in parse(reply):
        contact = _contact(row, company.strip())
        if contact is not None:
            found.setdefault(contact.id, contact)
    return list(found.values())


def address(contact: Contact) -> Optional[Contact]:
    """The same person with an address on them, or None. Spends no credit."""
    if contact.email:
        return contact
    if not contact.name or not (contact.company or contact.linkedin_url):
        return None
    reply = _ask(ADDRESS_PROMPT.format(
        name=contact.name, title=contact.title or "unknown",
        company=contact.company or "unknown",
        linkedin=contact.linkedin_url or "unknown",
    ))
    rows = parse(reply)
    email = usable_email(rows[0].get("email", "")) if rows else ""
    if not email:
        return None
    evidence = str(rows[0].get("evidence") or "").strip()
    return contact.model_copy(update={
        "email": email,
        "verification": "guessed",
        "email_status": f"found by Claude on {evidence}"[:200],
    })
