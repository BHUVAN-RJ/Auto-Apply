"""The screen's rules: what counts as a reject, decided in code.

Screening used to be a model call. Measured over the 122 postings on disk
against the 199 verdicts that model had cached, the rules below won every
disagreement: seven postings it passed are hard export-control blocks
(ITAR, EAR, "U.S. Person Required"), and six it rejected were a form's own
sponsorship question or a US onsite line, both of which the policy has
always called green. A screen is a handful of phrases read against a
handful of facts, and phrases are what code is good at.

A rule is a category, a severity, and either a set of patterns or the name
of a check that needs more than a pattern (a year count, a date window, a
place). Every rule can be switched off, and a person can add their own; the
catalog here is the stock set and `data/screening.json` holds what they
changed. Nothing in this module calls a model. The Screening tab's
assistant writes *rules*, once, on request - never a verdict per posting.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

import paths

# The categories a flag can carry, shared with `tailor.screen`.
CATEGORIES = (
    "experience", "visa", "export_control", "clearance", "timeline",
    "location", "degree", "seniority", "perm", "compensation", "other",
)
SEVERITIES = ("hard", "soft")

STORE_PATH = Path(os.environ.get("AUTOPILOT_SCREENING", paths.DATA / "screening.json"))

# How much of the posting around a match is shown as the quote. Long enough
# to read as a sentence, short enough that the banner stays one line or two.
QUOTE_CHARS = 160


@dataclass
class Rule:
    """One thing worth noticing in a posting.

    `patterns` fire the rule; `unless` cancels a match whose own sentence
    also says one of these (a posting that says "we will sponsor" must not
    trip the rule that looks for sponsorship words). `check` names a
    function in CHECKS for the rules that need more than a phrase, and
    `value` is that check's one number.
    """

    name: str
    label: str
    category: str
    severity: str
    note: str
    patterns: list[str] = field(default_factory=list)
    unless: list[str] = field(default_factory=list)
    check: str = ""
    value: Optional[int] = None
    enabled: bool = True
    origin: str = "stock"

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------- the words
#
# Each pattern is searched over the whole posting, case-insensitively. They
# are deliberately written around the verb rather than the noun: "sponsor"
# alone appears in postings that offer sponsorship, in EEO boilerplate and
# in the name of a team.

SPONSORSHIP_REFUSED = [
    r"\b(?:un(?:able|willing)|not\s+able|do(?:es)?\s+not|will\s+not|cannot|can\s?not|won'?t)\b"
    r"[^.\n]{0,60}?\b(?:sponsor|sponsorship|visa)\b",
    r"\bno\s+(?:visa\s+|employment\s+|immigration\s+)?sponsorship\b",
    r"\bsponsorship\s+is\s+not\s+(?:available|offered|provided|possible)\b",
    r"\bwithout\s+(?:the\s+)?(?:need\s+for\s+)?(?:current\s+or\s+future\s+)?"
    r"(?:visa\s+|employment\s+)?sponsorship\b",
    r"\bnot\s+(?:require|need)\s+(?:visa\s+|employment\s+)?sponsorship\b",
    r"\bineligible\s+for\s+(?:visa\s+)?sponsorship\b",
    r"\bauthoriz(?:ed|ation)\s+to\s+work[^.\n]{0,40}\bwithout\s+sponsorship\b",
]
# A hedge is not a refusal. "We cannot guarantee sponsorship for every role"
# and "this does not guarantee sponsorship for this specific role" appear in
# postings that sponsor; reading them as a block cost three good jobs on the
# corpus.
SPONSORSHIP_HEDGE = [
    r"\bguarantee\b",
    r"\bevery\s+(?:role|candidate|position)\b",
    r"\bcase[-\s]by[-\s]case\b",
    r"\bdepend(?:s|ing)?\s+on\b",
]
SPONSORSHIP_OFFERED = [
    r"\bwill\s+sponsor\b", r"\bwe\s+sponsor\b", r"\bdo\s+sponsor\b",
    r"\bhappy\s+to\s+sponsor\b", r"\bopen\s+to\s+sponsor(?:ing|ship)\b",
    # "sponsorship is available" only counts when nothing earlier in the
    # sentence negates it: "No visa sponsorship is available" contains the
    # same words and means the opposite.
    r"^(?:(?!\b(?:no|not|cannot|unable|never|without)\b).)*"
    r"\bsponsorship\s+(?:is\s+)?(?:available|offered|provided)\b",
    r"\bwill\s+consider\s+(?:visa\s+)?sponsorship\b",
    r"\bh-?1b\s+(?:transfer|sponsorship)\s+(?:available|offered)\b",
]

CITIZEN_ONLY = [
    r"\b(?:u\.?\s?s\.?|united\s+states)\s+citizens?(?:hip)?\s+(?:only|required|is\s+required)\b",
    r"\bmust\s+be\s+a\s+(?:u\.?\s?s\.?|united\s+states)\s+citizen\b",
    r"\bonly\s+(?:u\.?\s?s\.?|united\s+states)\s+citizens\b",
    r"\brestricted\s+to\s+(?:u\.?\s?s\.?|united\s+states)\s+citizens\b",
]

CLEARANCE = [
    r"\b(?:active|current|existing)?\s*(?:security|dod|doe|government)\s+clearance\b",
    r"\bts\s*/\s*sci\b",
    r"\btop\s+secret\b",
    r"\bsecret\s+clearance\b",
    r"\bpublic\s+trust\b",
    r"\bpolygraph\b",
    r"\b(?:able|ability)\s+to\s+obtain\s+(?:and\s+maintain\s+)?a?\s*(?:security\s+)?clearance\b",
    r"\bclearance\s+(?:is\s+)?required\b",
]
# A posting that says no clearance is needed names the clearance to say so:
# "No Clearance: Position does not require a security clearance." fired the
# rule as a hard reject (2026-10-04). The lookahead keeps the cancel off a
# sentence that also asks for one to be obtained or held later - "does not
# require an active clearance, but must be able to obtain one" still fires.
_CLEARANCE_STILL_WANTED = r"^(?!.*\b(?:obtain|eligib\w*|maintain|acquire)\b)"
CLEARANCE_NOT_REQUIRED = [
    _CLEARANCE_STILL_WANTED + r".*\bno\s+(?:\w+\s+){0,2}clearance\b",
    _CLEARANCE_STILL_WANTED + r".*\b(?:does|do|will)\s+not\s+(?:require|need)\b[^.\n]{0,50}\bclearance\b",
    _CLEARANCE_STILL_WANTED + r".*\bclearance\s+(?:is\s+)?not\s+(?:required|needed|necessary)\b",
    _CLEARANCE_STILL_WANTED + r".*\bclearance(?:\s+level)?(?:\s+required)?\s*[:\-–—]\s*(?:none|no|n/?a|not\s+required)\b",
]

# Named regimes and the "U.S. Person" requirement are a block: an F-1 holder
# is not a US person and no amount of willingness changes it.
EXPORT_CONTROL_HARD = [
    r"\bitar\b",
    r"\binternational\s+traffic\s+in\s+arms\b",
    r"\bexport\s+administration\s+regulations\b",
    r"\bear\s+99\b",
    r"\bu\.?\s?s\.?\s+person(?:s)?\s+(?:required|status\s+required|only)\b",
    r"\bmust\s+(?:be|qualify\s+as)\s+an?\s+(?:u\.?\s?s\.?\s+)?person\s+(?:as\s+defined|under)\b",
    r"\bu\.?\s?s\.?\s+person\s+required\b",
]
# A paragraph that only mentions export control or sanctions is usually the
# OFAC-country boilerplate every large employer carries, which says nothing
# about this candidate. Worth reading, not worth rejecting.
EXPORT_CONTROL_SOFT = [
    r"\bexport[-\s]control(?:led|s)?\b",
    r"\bexport\s+compliance\b",
    r"\b(?:u\.?\s?s\.?\s+)?sanctions\b",
]

PHD = [
    r"\b(?:ph\.?\s?d\.?|doctorate|doctoral\s+degree)\b[^.\n]{0,40}\b(?:required|is\s+required|mandatory)\b",
    r"\b(?:requires?|must\s+have|must\s+hold)\b[^.\n]{0,30}\b(?:ph\.?\s?d\.?|doctorate)\b",
]

PERM_AD = [
    r"\bnotice\s+of\s+filing\b",
    r"\bmail\s+resumes?\s+to\b",
    r"\bjob\s+(?:code|order)\s*#?\s*\d+\b",
    r"\bprevailing\s+wage\s+determination\b",
    r"\bin\s+response\s+to\s+this\s+notice\b",
]

CONTRACT_ONLY = [
    r"\bcorp[-\s]?to[-\s]?corp\b", r"\bc2c\b", r"\b1099\s+(?:only|contract)\b",
    r"\bw2\s+only\b", r"\bthird[-\s]party\s+(?:agencies|vendors)\s+only\b",
]


STOCK: tuple[Rule, ...] = (
    Rule(
        name="sponsorship_refused",
        label="The posting refuses visa sponsorship",
        category="visa",
        severity="hard",
        note=("The single most common real reject. Written around the refusal "
              "verb, and cancelled when the same sentence offers sponsorship - "
              "the model once rejected a job over the words \"Will sponsor\"."),
        patterns=SPONSORSHIP_REFUSED,
        unless=SPONSORSHIP_OFFERED + SPONSORSHIP_HEDGE,
    ),
    Rule(
        name="citizen_only",
        label="Open to US citizens only",
        category="visa",
        severity="hard",
        note=("Citizenship as a requirement, not the E-Verify and "
              "work-eligibility boilerplate every US posting carries."),
        patterns=CITIZEN_ONLY,
    ),
    Rule(
        name="clearance_required",
        label="A security clearance is required",
        category="clearance",
        severity="hard",
        note=("Including \"able to obtain\", which needs citizenship in practice. "
              "Cancelled when the same sentence says none is required."),
        patterns=CLEARANCE,
        unless=CLEARANCE_NOT_REQUIRED,
    ),
    Rule(
        name="export_control",
        label="Export control: ITAR, EAR, or \"U.S. Person\"",
        category="export_control",
        severity="hard",
        note=("Raised 2026-09-21 and never caught: the model passed seven of "
              "these. An export-controlled role is closed to an F-1 visa "
              "holder whatever the posting says about sponsorship."),
        patterns=EXPORT_CONTROL_HARD,
    ),
    Rule(
        name="export_control_mentioned",
        label="Export control or sanctions mentioned",
        category="export_control",
        severity="soft",
        note=("The wider paragraph, without a named regime. Usually the "
              "OFAC-country boilerplate a large employer carries; read it "
              "before you decide."),
        patterns=EXPORT_CONTROL_SOFT,
    ),
    Rule(
        name="phd_required",
        label="A PhD is required",
        category="degree",
        severity="hard",
        note="\"PhD or equivalent experience\" is not this; the word required is.",
        patterns=PHD,
    ),
    Rule(
        name="perm_ad",
        label="A PERM advertisement, not a real opening",
        category="perm",
        severity="hard",
        note=("A notice filed for someone already hired. The tells are the "
              "filing language and a job code, never the job description."),
        patterns=PERM_AD,
    ),
    Rule(
        name="contract_only",
        label="Corp-to-corp or agency only",
        category="other",
        severity="soft",
        note="Not a block, but rarely worth an application from a candidate.",
        patterns=CONTRACT_ONLY,
        enabled=False,
    ),
    Rule(
        name="experience_years",
        label="Asks for more years of experience than you have",
        category="experience",
        severity="hard",
        note=("Reads the first number of every \"N years of experience\" in the "
              "posting and flags the lowest requirement at or above the value. "
              "A range counts as its floor, so \"0-1 years\" never fires."),
        check="experience_years",
        value=4,
    ),
    Rule(
        name="seniority_title",
        label="A senior title",
        category="seniority",
        severity="hard",
        note="Read off the job title only, word-bounded, never the description.",
        check="seniority_title",
    ),
    Rule(
        name="location_foreign",
        label="The role is outside the United States",
        category="location",
        severity="hard",
        note=("Only a place that is unambiguously another country. Every US "
              "location is green, onsite or not, because relocation is open."),
        check="location_foreign",
    ),
    Rule(
        name="graduation_window",
        label="A graduation date you cannot meet",
        category="timeline",
        severity="hard",
        note=("Cohort windows and \"degree by\" cutoffs, read against your own "
              "graduation. Graduating earlier is a match, never a caution."),
        check="graduation_window",
    ),
)

BY_NAME = {rule.name: rule for rule in STOCK}


# ------------------------------------------------------------------ store


def _read() -> dict:
    if not STORE_PATH.exists():
        return {}
    try:
        data = json.loads(STORE_PATH.read_text() or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _write(data: dict) -> None:
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STORE_PATH.write_text(json.dumps(data, indent=2) + "\n")


def rules() -> list[Rule]:
    """The stock catalog with the person's switches and values applied, then
    their own rules. Read at call time: a rule added on the page is used by
    the next screen without a restart."""
    saved = _read()
    off = set(saved.get("disabled") or [])
    values = saved.get("values") or {}
    out: list[Rule] = []
    for rule in STOCK:
        copy = Rule(**rule.to_dict())
        copy.enabled = rule.name not in off
        if rule.name in values and isinstance(values[rule.name], int):
            copy.value = values[rule.name]
        out.append(copy)
    for item in saved.get("custom") or []:
        try:
            custom = Rule(**{**item, "origin": "yours"})
        except TypeError:
            continue
        custom.enabled = custom.name not in off
        out.append(custom)
    return out


def get(name: str) -> Optional[Rule]:
    return next((r for r in rules() if r.name == name), None)


def set_enabled(name: str, enabled: bool) -> list[Rule]:
    saved = _read()
    off = set(saved.get("disabled") or [])
    off.discard(name) if enabled else off.add(name)
    saved["disabled"] = sorted(off)
    _write(saved)
    return rules()


def set_value(name: str, value: int) -> list[Rule]:
    saved = _read()
    values = dict(saved.get("values") or {})
    values[name] = int(value)
    saved["values"] = values
    _write(saved)
    return rules()


def save_custom(rule: Rule) -> list[Rule]:
    """Add or replace one of the person's own rules. A stock name is refused:
    the catalog is what an update ships, and a rule that shadowed it would
    silently diverge from the note explaining it."""
    if rule.name in BY_NAME:
        raise ValueError(f"{rule.name} is a stock rule; switch it off or pick another name")
    problem = invalid(rule)
    if problem:
        raise ValueError(problem)
    saved = _read()
    custom = [c for c in (saved.get("custom") or []) if c.get("name") != rule.name]
    rule.origin = "yours"
    custom.append(rule.to_dict())
    saved["custom"] = custom
    _write(saved)
    return rules()


def delete_custom(name: str) -> list[Rule]:
    saved = _read()
    saved["custom"] = [c for c in (saved.get("custom") or []) if c.get("name") != name]
    _write(saved)
    return rules()


NAME_SHAPE = re.compile(r"^[a-z][a-z0-9_]{2,40}$")


def invalid(rule: Rule) -> str:
    """Why this rule cannot be saved, or "". Every pattern must compile, and
    a rule must be able to fire on something: a pattern or a known check."""
    if not NAME_SHAPE.match(rule.name or ""):
        return "the name must be lower case letters, digits and underscores"
    if rule.category not in CATEGORIES:
        return f"unknown category {rule.category!r}"
    if rule.severity not in SEVERITIES:
        return f"severity must be hard or soft, not {rule.severity!r}"
    if rule.check and rule.check not in CHECKS:
        return f"unknown check {rule.check!r}"
    if not rule.patterns and not rule.check:
        return "a rule needs at least one pattern"
    for pattern in list(rule.patterns) + list(rule.unless):
        try:
            re.compile(pattern, re.I)
        except re.error as exc:
            return f"{pattern!r} is not a valid regular expression: {exc}"
        if _matches_everything(pattern):
            return f"{pattern!r} matches almost any text; make it more specific"
    return ""


def _matches_everything(pattern: str) -> bool:
    """A pattern that fires on ordinary prose would reject every job. Cheap
    guard: try it on a paragraph with nothing worth flagging in it."""
    harmless = (
        "We are looking for a software engineer to join our platform team in "
        "Chicago. You will build services in Python and Go, work with a small "
        "group of engineers, and help us scale. We offer health insurance and "
        "a yearly learning budget."
    )
    try:
        return re.search(pattern, harmless, re.I) is not None
    except re.error:
        return False


# ----------------------------------------------------------------- reading

SENTENCE_SPLIT = re.compile(r"(?<=[.!?\n])\s+")
QUESTION_WORDS = re.compile(
    r"^\s*(?:do|does|did|are|is|will|would|can|could|have|has|what|which|"
    r"who|when|where|why|how)\b", re.I)


# A sentence ends at ? or ! or a newline, or at a full stop that is not
# part of an abbreviation. Splitting on every full stop cut "require
# sponsorship (i.e." off from its own question mark, and a question read as
# a statement is the exact false positive the model used to make.
SENTENCE_END = re.compile(r"[?!\n]|\.(?=\s|$)(?<!\b[A-Z]\.)(?<!\bi\.e\.)(?<!\be\.g\.)")


def sentence_around(text: str, start: int, end: int) -> str:
    """The sentence a match sits in, squashed to one line."""
    left = 0
    for match in SENTENCE_END.finditer(text, 0, start):
        left = match.end()
    right = len(text)
    after = SENTENCE_END.search(text, end)
    if after:
        right = after.end()
    return " ".join(text[left:right].split())


# An answer, not a policy. Ashby renders a question and its options as plain
# lines, so the text a screen reads carries both - and while the question
# itself is passed over by `is_question`, the option under it is a flat
# statement: "No, I do not require visa sponsorship to work in the United
# States". That sentence read as the employer refusing sponsorship, and a
# posting captured on its application page was rejected on the strength of
# the candidate's own answer (2026-09-30).
# The comma is what separates an answer from a policy: "No, I do not require
# sponsorship" is a candidate answering, "No visa sponsorship is available"
# is an employer refusing, and both open with the same word.
ANSWER_OPENS = re.compile(
    r"^\s*(?:(?:yes|no|n/?a|not\s+applicable)\s*[,:;—-]|i\s+(?:do|am|have|will|would)\b)", re.I)


def is_answer(text: str, start: int, sentence: str) -> bool:
    """Whether this match is somebody answering a question rather than an
    employer stating a policy.

    Two signs, either is enough: the sentence opens the way an answer opens
    ("Yes, ...", "No, I do not ..."), or the line above it is a question. A
    form lists its options directly under the question they belong to, which
    is the shape this reads.
    """
    if ANSWER_OPENS.match(sentence):
        return True
    head = text.rfind("\n", 0, start)
    while head > 0:
        above = text[text.rfind("\n", 0, head - 1) + 1:head].strip()
        if above:
            return above.endswith("?")
        head = text.rfind("\n", 0, head - 1)
    return False


def is_question(sentence: str) -> bool:
    """A form's own question is not a requirement.

    "Will you now or in the future require sponsorship?" is on half the
    application forms in the country and says nothing about the employer's
    policy. The model flagged it as a hard reject four separate times.
    """
    return sentence.rstrip().endswith("?") or bool(
        QUESTION_WORDS.match(sentence) and "?" in sentence)


def first_match(text: str, rule: Rule) -> Optional[str]:
    """The quote this rule fires on, or None. A match whose sentence is a
    question, or which its own `unless` cancels, is passed over rather than
    ending the search: a posting often asks the question and states the
    policy."""
    for pattern in rule.patterns:
        try:
            compiled = re.compile(pattern, re.I)
        except re.error:
            continue
        for match in compiled.finditer(text):
            sentence = sentence_around(text, match.start(), match.end())
            if not sentence or is_question(sentence):
                continue
            if is_answer(text, match.start(), sentence):
                continue
            if any(re.search(p, sentence, re.I) for p in rule.unless):
                continue
            return sentence[:QUOTE_CHARS]
    return None


# ------------------------------------------------------------- the checks
#
# Rules that need more than a phrase. Each takes the posting and returns a
# quote, or None. They are named in `Rule.check` and listed in CHECKS, so a
# person's own rule can reuse one.

YEARS = re.compile(
    r"\b(\d{1,2})\s*(?:\+|or\s+more)?\s*(?:[-–]|to)?\s*(\d{1,2})?\s*\+?\s*years?\b"
    r"[^.\n]{0,60}?\b(?:experience|exp\b)", re.I)


def check_experience_years(text: str, title: str, facts: str, rule: Rule) -> Optional[str]:
    """The lowest "N years of experience" the posting asks for, when it is at
    or above the rule's value. The floor of a range is what counts, so
    "0-1 years" and "1-3 years" never fire."""
    floor = rule.value if rule.value is not None else 4
    best: Optional[tuple[int, str]] = None
    for match in YEARS.finditer(text):
        years = int(match.group(1))
        sentence = sentence_around(text, match.start(), match.end())
        if is_question(sentence):
            continue
        if best is None or years < best[0]:
            best = (years, sentence[:QUOTE_CHARS])
    if best and best[0] >= floor:
        return best[1]
    return None


SENIOR_TITLE = re.compile(
    r"(?<![a-z])(?:senior|sr\.?|staff|principal|lead|director|head\s+of|"
    r"vp|vice\s+president|distinguished|fellow|iv|v)(?![a-z0-9])", re.I)
# Titles that carry a senior word but name an entry-level role. "Member of
# Technical Staff" is the new-grad title at half the AI labs; "Associate"
# and the campus words mean the opposite of senior wherever they appear.
NOT_SENIOR_TITLE = re.compile(
    r"(?<![a-z])(?:member\s+of\s+technical\s+staff|mts|associate|assistant|"
    r"new\s+grad(?:uate)?|university|campus|early\s+career|entry[-\s]level|"
    r"intern|apprentice|junior|jr\.?|rotational)(?![a-z0-9])", re.I)


def check_seniority_title(text: str, title: str, facts: str, rule: Rule) -> Optional[str]:
    """Read off the title alone, and never when the title also names an
    entry-level role. The description says "work with senior engineers" in
    half the postings written for new graduates, and "Member of Technical
    Staff" is what Cerebras and Perplexity call the job they hire them into.
    """
    title = (title or "").strip()
    if not title or NOT_SENIOR_TITLE.search(title):
        return None
    return f"Title: {title}" if SENIOR_TITLE.search(title) else None


def check_location_foreign(text: str, title: str, facts: str, rule: Rule) -> Optional[str]:
    """A posting whose places are all outside the United States.

    `scout.filter` already owns the country lists; this reuses them rather
    than keeping a second copy that drifts. Anything unknown, empty, remote
    or American keeps the job.
    """
    from scout import filter as place

    head = " ".join(text[:1500].split())
    for line in head.split(". "):
        lowered = line.lower()
        if "remote" in lowered and place.outside_us(lowered) is False:
            return None
    return f"Location: {head[:QUOTE_CHARS]}" if place.outside_us(head.lower()) else None


def check_graduation_window(text: str, title: str, facts: str, rule: Rule) -> Optional[str]:
    """A cohort window or a "degree by" cutoff the applicant misses.

    `tailor.screen` owns the date reading, including the rule that
    graduating early is a match; this only finds the sentences worth
    handing to it.
    """
    from . import screen as screener

    graduation = screener.latest_graduation(facts)
    for sentence in SENTENCE_SPLIT.split(text):
        flat = " ".join(sentence.split())
        if not flat or is_question(flat):
            continue
        if not screener.GRADUATION_WORDS.search(flat):
            continue
        # A posting that wants a graduation *no earlier* than some date is
        # the one case an early graduate genuinely fails: an internship or
        # co-op that needs them still enrolled.
        if screener.NOT_BEFORE.search(flat):
            earliest = _earliest(flat)
            if earliest and graduation and graduation < earliest:
                return flat[:QUOTE_CHARS]
            continue
        latest = screener.graduation_deadline(flat) or screener.graduation_window(flat)
        if latest is None:
            continue
        if graduation is None or graduation > latest:
            return flat[:QUOTE_CHARS]
    return None


def _earliest(quote: str) -> Optional[tuple[int, int]]:
    """The date a "no earlier than" sentence names, as (year, month)."""
    from . import screen as screener

    years = [int(y) for y in re.findall(r"\b(20\d{2})\b", quote)]
    if not years:
        return None
    months = [screener.MONTH_NUMBERS[w.lower()]
              for w in re.findall(rf"\b({screener.DATE_WORDS})\b", quote, re.I)]
    return min(years), min(months) if months else 1


CHECKS: dict[str, Callable[[str, str, str, Rule], Optional[str]]] = {
    "experience_years": check_experience_years,
    "seniority_title": check_seniority_title,
    "location_foreign": check_location_foreign,
    "graduation_window": check_graduation_window,
}


def fired(text: str, title: str = "", facts: str = "",
          catalog: Optional[list[Rule]] = None) -> list[tuple[Rule, str]]:
    """Every enabled rule that fires on this posting, with its quote."""
    hits: list[tuple[Rule, str]] = []
    for rule in (catalog if catalog is not None else rules()):
        if not rule.enabled:
            continue
        quote = None
        if rule.check:
            check = CHECKS.get(rule.check)
            if check is not None:
                quote = check(text, title, facts, rule)
        if quote is None and rule.patterns:
            quote = first_match(text, rule)
        if quote:
            hits.append((rule, quote))
    return hits


# ---------------------------------------------------------------- the author
#
# The Screening tab's assistant. It writes a rule from a sentence like "flag
# anything that wants a security clearance", once, on request; it never sees
# a posting and never produces a verdict. The rule it returns is validated
# here, previewed against the postings already on disk, and saved only when
# the person presses the button.

AUTHOR_PROMPT = """
You write screening rules for one person's job search. A rule is a small
piece of JSON that a program runs over a job posting's text; it fires when
one of its regular expressions matches, and the person then sees the
sentence it matched.

Write regular expressions the way a careful engineer would:

- Match the phrase an employer would actually write, not a single word.
  `sponsor` appears in postings that offer sponsorship; `unable to sponsor`
  does not.
- Anchor with `\\b` word boundaries. An unanchored `ear` matches "year",
  "learn" and "research".
- Prefer several narrow patterns over one wide one. Every pattern in the
  list fires the rule on its own.
- Put the phrases that cancel a match into `unless`. They are checked
  against the same sentence, so `guarantee` in `unless` stops a rule about
  refusing sponsorship from firing on "we cannot guarantee sponsorship".
- Never write a pattern that would match ordinary prose. A rule that fires
  on every posting is worse than no rule.

Severity is `hard` when the person cannot be hired for the role whatever
they do (citizenship, clearance, a closed export-controlled position), and
`soft` when it is worth reading before applying. Categories are limited to:
experience, visa, export_control, clearance, timeline, location, degree,
seniority, perm, compensation, other.

If the request is not a screening rule, or needs facts about the person you
were not given, say so in `problem` and return no rule.
"""

AUTHOR_FORMAT = """
Reply with one fenced json block and nothing else:

```json
{
  "rule": {
    "name": "<lower_case_with_underscores, three to forty characters>",
    "label": "<what the checkbox says, a short sentence>",
    "category": "<one of the categories above>",
    "severity": "hard | soft",
    "note": "<why this rule exists and what it deliberately does not catch>",
    "patterns": ["<regex>", "..."],
    "unless": ["<regex that cancels a match in the same sentence>"]
  },
  "explain": "<one or two sentences for the person, plain English>",
  "problem": "<why you wrote no rule, or an empty string>"
}
```
"""
