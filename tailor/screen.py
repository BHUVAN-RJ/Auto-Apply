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

from . import llm, profile, prompts

ROOT = Path(__file__).resolve().parent.parent
RULES = Path(__file__).resolve().parent / "screen_rules.md"

CATEGORIES = (
    "experience", "visa", "export_control", "clearance", "timeline",
    "location", "degree", "seniority", "perm", "other",
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
        flags = enforce_location_policy([Flag(**f) for f in data.get("flags", [])])
        verdict = data["verdict"]
        if verdict != "not_a_job":
            verdict = verdict_for(flags)
        return cls(
            verdict=verdict,
            flags=flags,
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
    return prompts.text("screen") + "\n" + prompts.text("screen.format")


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


US_STATE_NAMES = (
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana",
    "maine", "maryland", "massachusetts", "michigan", "minnesota",
    "mississippi", "missouri", "montana", "nebraska", "nevada",
    "new hampshire", "new jersey", "new mexico", "new york",
    "north carolina", "north dakota", "ohio", "oklahoma", "oregon",
    "pennsylvania", "rhode island", "south carolina", "south dakota",
    "tennessee", "texas", "utah", "vermont", "virginia", "washington",
    "west virginia", "wisconsin", "wyoming", "district of columbia",
)
US_STATE_CODES = (
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
    "DC",
)


def quote_names_us_location(quote: str) -> bool:
    """Whether a location quote explicitly identifies the United States.

    City-to-city distance inside the US is never a screen flag. State codes
    stay case-sensitive here so ordinary words such as "in" and "or" do not
    accidentally look like Indiana and Oregon.
    """
    lowered = quote.lower()
    if re.search(r"\bunited states(?: of america)?\b", lowered):
        return True
    if re.search(r"\bU\.?S\.?(?:A\.?)?\b", quote) or re.search(r"\bUSA\b", quote):
        return True
    if any(re.search(rf"\b{re.escape(state)}\b", lowered) for state in US_STATE_NAMES):
        return True
    codes = "|".join(US_STATE_CODES)
    return bool(re.search(rf"(?:,|\b(?:in|at))\s*(?:{codes})(?:\s+\d{{5}})?\b", quote))


def enforce_location_policy(flags: list[Flag]) -> list[Flag]:
    """Remove location flags that cannot represent a foreign-country block.

    Under the screening policy, a US location is always green and a genuine
    foreign-country restriction is hard. Consequently a soft location flag
    is invalid regardless of the model's explanation.
    """
    return [
        flag for flag in flags
        if flag.category != "location"
        or (flag.severity == "hard" and not quote_names_us_location(flag.quote))
    ]


MONTH_NUMBERS = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    "spring": 5, "summer": 8, "fall": 11, "autumn": 11, "winter": 2,
}
DATE_WORDS = "|".join(MONTH_NUMBERS)


def latest_graduation(facts: str) -> Optional[tuple[int, int]]:
    """Most recent graduation as (year, month), from degree-related facts.

    A year without a month is treated as December. That conservative choice
    only suppresses a flag when even the latest date in that year meets the
    posting's deadline.
    """
    dates: list[tuple[int, int]] = []
    for line in facts.splitlines():
        if not re.search(r"\b(?:degree|graduat)", line, re.I):
            continue
        specific = re.findall(rf"\b({DATE_WORDS})\s+(?:of\s+)?(20\d{{2}})\b", line, re.I)
        if specific:
            dates.extend((int(year), MONTH_NUMBERS[word.lower()]) for word, year in specific)
        else:
            dates.extend((int(year), 12) for year in re.findall(r"\b(20\d{2})\b", line))
    return max(dates) if dates else None


def graduation_deadline(quote: str) -> Optional[tuple[int, int]]:
    """Latest allowed graduation from phrases such as "by Summer 2027"."""
    match = re.search(
        rf"\b(?:by|no later than|on or before)\s+(?:the end of\s+)?"
        rf"(?:(?P<word>{DATE_WORDS})\s+(?:of\s+)?)?(?P<year>20\d{{2}})\b",
        quote,
        re.I,
    )
    if not match:
        return None
    word = match.group("word")
    return int(match.group("year")), MONTH_NUMBERS[word.lower()] if word else 12


# A posting that wants a graduation no earlier than some date is the one
# timeline case an early graduate genuinely fails: an internship or co-op
# that needs the applicant still enrolled. Those words keep the flag.
NOT_BEFORE = re.compile(
    r"\b(?:or later|after|no earlier than|at least|still (?:be )?enrolled|"
    r"currently enrolled|returning to)\b",
    re.I,
)
GRADUATION_WORDS = re.compile(r"\b(?:graduat\w*|class of|degree|commencement)", re.I)


def graduation_window(quote: str) -> Optional[tuple[int, int]]:
    """Latest graduation a cohort window allows, as (year, month).

    Covers the windows a deadline phrase does not: "spring/summer of 2027
    college graduates", "Class of 2027", "graduating between Fall 2026 and
    Summer 2027". The window's end is the latest year named with the latest
    season or month named anywhere in the quote, which is generous by design:
    the applicant graduating on or before it is a match, and only a later
    graduation keeps the flag.
    """
    if not GRADUATION_WORDS.search(quote) or NOT_BEFORE.search(quote):
        return None
    years = [int(year) for year in re.findall(r"\b(20\d{2})\b", quote)]
    if not years:
        return None
    months = [
        MONTH_NUMBERS[word.lower()]
        for word in re.findall(rf"\b({DATE_WORDS})\b", quote, re.I)
    ]
    return max(years), max(months) if months else 12


def graduating_in_time(flag: Flag, graduation: tuple[int, int]) -> bool:
    """Does the applicant's graduation satisfy this flag's date, if it has one?

    Earlier than a cutoff or a cohort window is a match, not a caution: a
    December 2026 graduate is available for every role that asks for a degree
    by Summer 2027, and for every cohort that graduates by then.
    """
    for latest in (graduation_deadline(flag.quote), graduation_window(flag.quote)):
        if latest is not None and graduation <= latest:
            return True
    return False


def enforce_timeline_policy(flags: list[Flag], facts: str) -> list[Flag]:
    """Drop deadline and cohort-window flags the applicant's graduation meets."""
    graduation = latest_graduation(facts)
    if graduation is None:
        return flags
    return [
        flag for flag in flags
        if flag.category != "timeline" or not graduating_in_time(flag, graduation)
    ]


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


def parse_reply(reply: str, model: str = "", text: Optional[str] = None,
                facts: Optional[str] = None) -> Screen:
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

    flags = enforce_location_policy(flags)
    flags = enforce_timeline_policy(flags, facts or "")

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
    result = parse_reply(reply, model=model, text=text, facts=facts)
    result.facts_source = source
    return soften_unknowns(result)
