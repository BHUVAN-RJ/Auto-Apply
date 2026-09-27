"""On-page screening: is this posting an auto-reject for this applicant?

Reads the posting text against the applicant's Facts section and a set of
rules, and returns a verdict. No model is asked (2026-09-26): measured over
the 122 postings on disk against the 199 verdicts the model had cached, the
rules won every disagreement - seven export-control blocks it had passed,
six rejects it should never have raised. `tailor/screening.py` holds the
rules and the person's own edits to them; this module holds the policies
that correct a flag after it is raised, the date reading, and the shape of
a verdict.

The verdict is advice. Nothing in the pipeline changes status because of it.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Optional

from . import profile, screening

CATEGORIES = (
    "experience", "visa", "export_control", "clearance", "timeline",
    "location", "degree", "seniority", "perm", "other",
)
SEVERITIES = ("hard", "soft")
VERDICTS = ("reject", "caution", "ok", "not_a_job")

# Postings run a few thousand tokens; beyond this the text is a careers index
# or boilerplate, and the model has enough to judge either way.
MAX_TEXT_CHARS = 20_000


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


def load_facts() -> tuple[str, str]:
    """(facts, source). applicant.md's Facts when the profile switch is on
    and the file exists; otherwise facts derived once from the resume."""
    return profile.screen_facts()


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


# US places a posting names without naming a state or the country. A quote
# such as "Based in the NYC tri-state area" is a US-to-US distance and under
# the policy is green, but it carries no state name, no state code and no
# "United States", so it used to survive as a hard flag and reject the job
# outright (a real run: NYC tri-state against an applicant in Los Angeles).
US_METROS = (
    "nyc", "new york city", "tri-state", "tristate", "bay area",
    "silicon valley", "san francisco", "los angeles", "socal",
    "southern california", "northern california", "seattle", "portland",
    "austin", "dallas", "houston", "chicago", "boston", "atlanta",
    "denver", "miami", "philadelphia", "phoenix", "san diego", "san jose",
    "pittsburgh", "detroit", "minneapolis", "nashville", "charlotte",
    "raleigh", "research triangle", "salt lake city", "las vegas",
    "kansas city", "st. louis", "saint louis", "baltimore", "orlando",
    "tampa", "sacramento", "columbus", "cincinnati", "cleveland",
    "indianapolis", "milwaukee", "new orleans", "oklahoma city",
    "san antonio", "d.c. metro", "dc metro", "washington d.c.",
    "sf", "newark", "sunnyvale", "mountain view", "palo alto",
    "santa clara", "santa monica", "brooklyn", "manhattan", "queens",
    "bellevue", "redmond", "cambridge", "arlington", "plano", "irvine",
    "boulder", "durham", "madison", "ann arbor", "provo", "scottsdale",
)


def quote_names_us_location(quote: str) -> bool:
    """Whether a location quote explicitly identifies the United States.

    City-to-city distance inside the US is never a screen flag. State codes
    stay case-sensitive here so ordinary words such as "in" and "or" do not
    accidentally look like Indiana and Oregon; US metros are read by name,
    because a posting often names only the metro ("the NYC tri-state area").
    """
    lowered = quote.lower()
    # Word-bounded: "sf" must not match inside another word, and "queens"
    # must not be found in "Queensland".
    if any(re.search(rf"\b{re.escape(metro)}\b", lowered) for metro in US_METROS):
        return True
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


def summarise(flags: list["Flag"]) -> str:
    """One line, the way the banner wants it: what fired, worst first."""
    if not flags:
        return "No hard or soft flags found."
    hard = [f.category.replace("_", " ") for f in flags if f.severity == "hard"]
    soft = [f.category.replace("_", " ") for f in flags if f.severity == "soft"]
    parts = []
    if hard:
        parts.append("blocked on " + ", ".join(dict.fromkeys(hard)))
    if soft:
        parts.append("worth a look: " + ", ".join(dict.fromkeys(soft)))
    return "; ".join(parts) + "."


def screen(text: str, title: str = "", url: str = "", facts: Optional[str] = None,
           model: Optional[str] = None) -> Screen:
    """Judge one posting. `facts` defaults to base/applicant.md's Facts section.

    `model` is accepted and ignored: callers and the cache predate the rules
    and there is nothing to gain from making them all change at once.
    """
    if not text or not text.strip():
        return Screen(verdict="not_a_job", summary="empty page")
    source = "given"
    if facts is None:
        facts, source = load_facts()

    flags = [
        Flag(category=rule.category, severity=rule.severity, quote=quote, reason=rule.label)
        for rule, quote in screening.fired(text[:MAX_TEXT_CHARS], title=title, facts=facts)
    ]
    # The rules raise the flags; these two correct them, as they always have.
    # A US place is never a location block however a rule read it, and a
    # graduation earlier than a posting's window is a match, not a caution.
    flags = enforce_location_policy(flags)
    flags = enforce_timeline_policy(flags, facts)

    result = Screen(
        verdict=verdict_for(flags),
        flags=flags,
        summary=summarise(flags),
        model="rules",
        facts_source=source,
    )
    return soften_unknowns(result)
