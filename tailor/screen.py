"""On-page screening: is this posting an auto-reject for this applicant?

Reads the posting text and the applicant's Facts section, asks a fast model
for a JSON verdict, and validates it against a fixed category set. The model's
reply is never trusted raw: an unknown category, a missing quote, or a verdict
that disagrees with its own flags is corrected here, not displayed.

The verdict is advice. Nothing in the pipeline changes status because of it.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from . import llm, profile

ROOT = Path(__file__).resolve().parent.parent
RULES = Path(__file__).resolve().parent / "screen_rules.md"

CATEGORIES = (
    "experience", "visa", "export_control", "clearance", "timeline",
    "location", "degree", "seniority", "other",
)
SEVERITIES = ("hard", "soft")
VERDICTS = ("reject", "caution", "ok", "not_a_job")

# Postings run a few thousand tokens; beyond this the text is a careers index
# or boilerplate, and the model has enough to judge either way.
MAX_TEXT_CHARS = 20_000

DEFAULT_MODEL = "deepseek/deepseek-v4-flash"

REPLY_FORMAT = """
Reply with one fenced json block and nothing else:

```json
{
  "verdict": "reject | caution | ok | not_a_job",
  "flags": [
    {"category": "<one of the category names>",
     "severity": "hard | soft",
     "quote": "<the posting's own words, at most twenty>",
     "reason": "<why it applies to this applicant, one short sentence>"}
  ],
  "summary": "<one line, ten words at most>"
}
```
"""


class ScreenError(RuntimeError):
    pass


@dataclass
class Flag:
    category: str
    severity: str
    quote: str
    reason: str


@dataclass
class Screen:
    verdict: str
    flags: list[Flag] = field(default_factory=list)
    summary: str = ""
    model: str = ""
    # Where the applicant facts came from: "applicant.md" or "resume". The
    # banner says so, because facts derived from a resume know nothing about
    # visas or start dates and the reader should weigh the verdict that way.
    facts_source: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Screen":
        return cls(
            verdict=data["verdict"],
            flags=[Flag(**f) for f in data.get("flags", [])],
            summary=data.get("summary", ""),
            model=data.get("model", ""),
            facts_source=data.get("facts_source", ""),
        )


def screen_model() -> str:
    return os.environ.get("OPENROUTER_SCREEN_MODEL", DEFAULT_MODEL)


def load_facts() -> tuple[str, str]:
    """(facts, source). applicant.md's Facts when the profile switch is on
    and the file exists; otherwise facts derived once from the resume."""
    return profile.screen_facts()


def system_prompt() -> str:
    return RULES.read_text() + "\n" + REPLY_FORMAT


def build_user_message(text: str, facts: str, title: str = "", url: str = "") -> str:
    head = "\n".join(line for line in (f"Title: {title}" if title else "",
                                       f"URL: {url}" if url else "") if line)
    return (
        f"## Applicant facts\n\n{facts}\n\n"
        f"## Posting\n\n{head}\n\n{text[:MAX_TEXT_CHARS]}"
    )


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def quote_is_real(quote: str, text: str) -> bool:
    """The quote must be in the posting and must not be a question.

    Both rules are in the prompt too, and both were broken on the first run:
    the model invented "no statement about clearance" as a quote, and
    flagged "Will you now or in the future require sponsorship?", which
    every form asks. A flag the reader cannot find in the page is dropped.
    """
    if quote.rstrip().endswith("?"):
        return False
    squashed = _squash(quote)
    if len(squashed) < 4:
        return False
    return squashed in _squash(text)


def verdict_for(flags: list[Flag]) -> str:
    """The verdict must agree with the flags it sits on. Simple enough to
    compute here rather than trust the model's arithmetic."""
    if any(f.severity == "hard" for f in flags):
        return "reject"
    return "caution" if flags else "ok"


# Facts derived from a resume say nothing about these. A posting-side line
# ("unable to sponsor") is still worth showing, but it cannot be a hard
# reject when the applicant's side is unknown.
UNKNOWN_FROM_RESUME = ("visa", "clearance", "export_control", "timeline")


def soften_unknowns(result: "Screen") -> "Screen":
    if result.facts_source == "resume":
        for flag in result.flags:
            if flag.category in UNKNOWN_FROM_RESUME:
                flag.severity = "soft"
        if result.verdict != "not_a_job":
            result.verdict = verdict_for(result.flags)
    return result


def parse_reply(reply: str, model: str = "", text: Optional[str] = None) -> Screen:
    """Validate the model's JSON into a Screen. Bad flags are dropped, not shown.

    With `text`, every flag's quote is checked against the posting.
    """
    match = re.search(r"```json[^\n]*\n(.*?)```", reply, re.S)
    raw = match.group(1) if match else reply
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ScreenError(f"screen reply is not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ScreenError("screen reply is not an object")

    flags: list[Flag] = []
    for item in data.get("flags") or []:
        if not isinstance(item, dict):
            continue
        category = str(item.get("category", "")).strip().lower()
        severity = str(item.get("severity", "hard")).strip().lower()
        quote = str(item.get("quote", "")).strip()
        reason = str(item.get("reason", "")).strip()
        if category not in CATEGORIES:
            category = "other"
        if severity not in SEVERITIES:
            severity = "hard"
        if not quote or (text is not None and not quote_is_real(quote, text)):
            # The rules say no quote, no flag. A flag without the posting's
            # words cannot be checked by the reader and is dropped.
            continue
        flags.append(Flag(category, severity, quote, reason))

    verdict = str(data.get("verdict", "")).strip().lower()
    if verdict != "not_a_job":
        verdict = verdict_for(flags)

    summary = str(data.get("summary", "")).strip()
    return Screen(verdict=verdict, flags=flags, summary=summary, model=model)


def screen(text: str, title: str = "", url: str = "", facts: Optional[str] = None,
           model: Optional[str] = None) -> Screen:
    """Judge one posting. `facts` defaults to base/applicant.md's Facts section."""
    if not text or not text.strip():
        return Screen(verdict="not_a_job", summary="empty page")
    source = "given"
    if facts is None:
        facts, source = load_facts()
    model = model or screen_model()
    reply = llm.complete(
        system_prompt(),
        build_user_message(text, facts, title=title, url=url),
        model=model,
        temperature=0.0,
        max_tokens=4000,
        # A classification over a short text. Thinking here roughly triples
        # the latency of a banner that is meant to appear as the page opens.
        reasoning={"enabled": False},
    )
    result = parse_reply(reply, model=model, text=text)
    result.facts_source = source
    return soften_unknowns(result)
