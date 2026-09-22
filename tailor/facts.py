"""The facts interview: the first thing the profile chat does.

The resume says where the candidate studied and worked; it does not say
whether they can work in the country, whether they hold a clearance, what
level they are after, whether they would move, or when they could start.
Those are the lines the auto-reject screen compares postings against and
the lines the form fill needs, and until now they had to be typed into
`base/applicant.md` by hand.

This asks for them, one question at a time, in the same chat and voice as
the stories interview, and writes `base/applicant.md` at the end. The
questions are fixed, in code; the model's only job is to turn a spoken
answer ("I'm on OPT, I'll need sponsorship eventually") into the literal
line the file wants ("Work authorisation: authorised to work in the United
States on OPT; will need H-1B sponsorship in the future"), and to ask once
more when the answer did not settle it. What the resume already states
(location, degrees, graduation, contact details) is derived first and
offered for confirmation rather than asked cold.

State lives in `base/stories/_facts.json`. The file it writes is the only
thing outside `base/stories/` this touches.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterator, Optional

from . import llm, profile

STATE_NAME = "_facts.json"
# The preliminary interview's file (server/form.py). It already holds the
# authorisation, clearance, location, start date and graduation lines,
# so those questions are answered from it in code and never asked twice.
FORM = profile.ROOT / "base" / "form.json"

# One question per fact. `line` is the label in applicant.md; `derived`
# names the derived-facts line to offer as a starting point, if any.
QUESTIONS: list[dict] = [
    {"key": "authorisation", "line": "Work authorisation",
     "ask": "First, work authorisation. Are you a citizen, a permanent resident, on OPT or "
            "another visa? And will you need sponsorship at some point?"},
    {"key": "clearance", "line": "Security clearance",
     "ask": "Do you hold a security clearance, or would you be eligible for one?"},
    {"key": "level", "line": "Level", "derived": "Level",
     "ask": "What level are you applying for: entry, mid, senior? And is there a level you "
            "would never want to see, like staff or manager?"},
    {"key": "location", "line": "Location", "derived": "Location",
     "ask": "Where are you based right now, city and state?"},
    {"key": "relocation", "line": "Relocation",
     "ask": "Would you relocate for the right job? Anywhere in the country, only some cities, "
            "or not at all? And is remote or hybrid fine?"},
    {"key": "start", "line": "Earliest start date",
     "ask": "When is the earliest you could start?"},
    {"key": "graduation", "line": "Graduation", "derived": "Graduation",
     "ask": "When do you graduate, or when did you? Month and year."},
    {"key": "contact", "line": "Form details",
     "ask": "Last one, for application forms. I have your phone and links from the resume. "
            "Anything to add or change: portfolio, pronouns, how you usually say you heard "
            "about a job?"},
]

# Lines in the Facts section, in file order, with what fills each.
FACT_LINES = [
    ("Years of professional experience", "derived"),
    ("Level", "level"),
    ("Work authorisation", "authorisation"),
    ("Security clearance", "clearance"),
    ("Country", "derived"),
    ("Location", "location"),
    ("Relocation", "relocation"),
    ("Earliest start date", "start"),
    ("Degrees held", "derived"),
    ("Graduation", "graduation"),
]

SKIP = re.compile(r"^\s*(skip|pass|next|no idea|don'?t know|leave it)\b", re.I)
MAX_FOLLOWUPS = 1


@dataclass
class Facts:
    phase: str = "new"  # new | asking | done
    index: int = 0
    followups: int = 0
    derived: dict = field(default_factory=dict)  # label -> value, from the resume
    contacts: dict = field(default_factory=dict)  # label -> value, from the resume
    answers: dict = field(default_factory=dict)  # key -> {"line": ..., "raw": ...}
    transcript: list[dict] = field(default_factory=list)

    @classmethod
    def path(cls) -> Path:
        return profile.STORIES / STATE_NAME

    @classmethod
    def load(cls) -> "Facts":
        path = cls.path()
        if not path.exists():
            return cls()
        data = json.loads(path.read_text())
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    def save(self) -> None:
        path = self.path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2) + "\n")
        tmp.replace(path)

    def question(self) -> Optional[dict]:
        return QUESTIONS[self.index] if self.index < len(QUESTIONS) else None


def facts_model() -> str:
    return profile.derive_model()


CONTACT_PROMPT = """From this resume (LaTeX), list the contact details as json and nothing
else. Use empty strings for what is not there.

```json
{"Phone": "", "Email": "", "LinkedIn": "", "GitHub": "", "Portfolio": ""}
```"""

NORMALISE_PROMPT = """You turn a job applicant's spoken answer into one literal line for a
facts file that an automated screen compares against job postings. The
line must be plain, specific, and complete enough to judge a posting by:
no hedging, no prose, no second sentence unless it carries a fact.

The question asked, the label the line needs, what the resume already
suggested (may be empty), and the answer follow. Reply with one fenced
json block and nothing else:

```json
{"line": "<the line, without the label>", "ask": "<empty, or one short follow-up question if the answer did not settle it>"}
```

Rules:
- Work authorisation: name the status (citizen, permanent resident, OPT,
  H-1B, ...) and whether sponsorship will be needed, now or later.
- Security clearance: "none" plus eligibility when known.
- Level: the levels wanted, then what is out of scope, as in
  "entry level (new grad / junior). Anything titled senior, staff, lead,
  principal, or manager is out of scope."
- Location: "City, ST".
- Relocation: where they would move and whether remote or hybrid is fine.
- Earliest start date: a date or "immediately".
- Graduation: month and year, and whether it is in the past or the future.
- Form details: a json object instead of a line, keys among Phone,
  LinkedIn, GitHub, Portfolio, Pronouns, "How did you hear about us";
  only the keys the answer changes or adds.
- "Skip", "don't know", or an answer that is clearly not about the question:
  line "unknown", ask empty.
- Ask a follow-up only when a screen could not act on the line; at most one."""


def _parse(reply: str) -> dict:
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", reply, re.S) or re.search(r"(\{.*\})", reply, re.S)
    if not match:
        raise llm.LLMError("the facts model did not answer with json")
    return json.loads(match.group(1))


def derived_lines() -> dict:
    """Label -> value from the resume-derived facts (cached in profile)."""
    out = {}
    for line in profile.derived_facts().splitlines():
        if line.startswith("- ") and ":" in line:
            label, _, value = line[2:].partition(":")
            out[label.strip()] = value.strip()
    return out


def contacts() -> dict:
    reply = llm.complete(CONTACT_PROMPT, profile.BASE_RESUME.read_text(), model=facts_model(),
                         temperature=0.0, max_tokens=500, reasoning={"enabled": False})
    try:
        return {k: str(v).strip() for k, v in _parse(reply).items() if str(v).strip()}
    except (llm.LLMError, json.JSONDecodeError, AttributeError):
        return {}


def opening(facts: Facts) -> str:
    left = [q for q in QUESTIONS if q["key"] not in facts.answers]
    if len(left) < len(QUESTIONS):
        names = ", ".join(q["line"].lower() for q in left)
        return (f"Before the stories, a few facts the resume cannot tell me. Your form details "
                f"already cover {len(QUESTIONS) - len(left)} of {len(QUESTIONS)}; "
                f"{len(left)} left: {names}. Say skip to leave one blank.")
    return ("Before the stories, a few facts the resume cannot tell me: work authorisation, "
            "clearance, the level you are after, relocation, start date. Eight short questions; "
            "say skip to leave one blank.")


def _form(path: Optional[Path] = None) -> dict:
    path = path or FORM
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return {k: str(v).strip() for k, v in data.items() if isinstance(v, (str, int, float)) and str(v).strip()}


def from_form(path: Optional[Path] = None) -> dict:
    """Answers the preliminary interview already gave, as the literal lines
    the facts file wants, key -> {"line", "raw"}. No model: the form's
    values are fixed choices and short strings. Missing keys are asked."""
    form = _form(path)
    out: dict = {}
    status = form.get("us_status", "")
    authorised = form.get("work_authorized", "").lower()
    sponsorship = form.get("needs_sponsorship", "").lower()
    if status or authorised or sponsorship:
        bits = []
        if authorised in ("yes", "no"):
            bits.append(("authorised" if authorised == "yes" else "not authorised")
                        + " to work in the United States" + (f" on {status}" if status else ""))
        elif status:
            bits.append(status)
        if form.get("citizenship"):
            bits.append(f"citizen of {form['citizenship']}")
        if sponsorship in ("yes", "no"):
            bits.append("will need visa sponsorship now or in the future" if sponsorship == "yes"
                        else "will not need visa sponsorship")
        if form.get("authorization_expires"):
            bits.append(f"current authorisation ends {form['authorization_expires']}")
        out["authorisation"] = "; ".join(bits)
    if form.get("clearance"):
        out["clearance"] = form["clearance"].lower() if form["clearance"].lower().startswith("none") else form["clearance"]
    if form.get("city") and form.get("state"):
        out["location"] = f"{form['city']}, {form['state']}"
    if form.get("start_date"):
        out["start"] = form["start_date"]
    if form.get("graduation_year"):
        out["graduation"] = form["graduation_year"]
    details = {label: form[key] for label, key in (("Phone", "phone"), ("LinkedIn", "linkedin"),
                                                    ("GitHub", "github"), ("Portfolio", "website"),
                                                    ("Pronouns", "pronouns"), ("How did you hear about us", "how_heard"))
               if form.get(key)}
    if details:
        out["contact"] = details
    return {key: {"line": line, "raw": "from the preliminary interview"} for key, line in out.items()}


def settled(facts: Facts) -> int:
    """How many of the questions have a line, asked or taken from the form."""
    return sum(1 for q in QUESTIONS if q["key"] in facts.answers)


def _advance(facts: Facts) -> None:
    """Move `index` to the next question the form did not answer."""
    while facts.index < len(QUESTIONS) and QUESTIONS[facts.index]["key"] in facts.answers:
        facts.index += 1


def question_text(facts: Facts) -> str:
    """The current question, with the resume's suggestion when there is one."""
    q = facts.question()
    if q is None:
        return ""
    hint = facts.derived.get(q.get("derived", ""), "")
    if hint and hint.lower() != "unknown":
        return f"{q['ask']} The resume suggests: {hint}."
    return q["ask"]


def start() -> Facts:
    """Fresh facts interview. Derived facts and contacts come from the
    resume first, so the questions can offer them."""
    facts = Facts(phase="asking")
    try:
        facts.derived = derived_lines()
    except Exception:  # noqa: BLE001 - the questions work without hints
        facts.derived = {}
    try:
        facts.contacts = contacts()
    except Exception:  # noqa: BLE001
        facts.contacts = {}
    facts.answers = from_form()
    _advance(facts)
    if facts.question() is None:
        facts.phase = "done"
        facts.transcript = [{"role": "assistant", "content": "The form details cover every fact; file written."}]
        facts.save()
        write_applicant(facts)
        return facts
    first = f"{opening(facts)}\n\n{question_text(facts)}"
    facts.transcript = [{"role": "assistant", "content": first}]
    facts.save()
    return facts


def normalise(facts: Facts, answer: str) -> dict:
    q = facts.question()
    if SKIP.match(answer):
        return {"line": "unknown", "ask": ""}
    user = (f"Question: {q['ask']}\nLabel: {q['line']}\n"
            f"Resume suggested: {facts.derived.get(q.get('derived', ''), '')}\n"
            f"Answer: {answer}")
    reply = llm.complete(NORMALISE_PROMPT, user, model=facts_model(), temperature=0.0,
                         max_tokens=400, reasoning={"enabled": False})
    data = _parse(reply)
    return {"line": data.get("line", ""), "ask": str(data.get("ask") or "").strip()}


def turn(facts: Facts, message: str) -> Iterator[dict]:
    """One answer in. Yields delta events for the page (spoken), and a
    final `facts` event with the phase; the caller decides what follows."""
    q = facts.question()
    if q is None:
        facts.phase = "done"
        facts.save()
        yield {"type": "facts", "phase": facts.phase}
        return
    facts.transcript.append({"role": "user", "content": message})
    result = normalise(facts, message)
    if result["ask"] and facts.followups < MAX_FOLLOWUPS and result["line"] in ("", "unknown"):
        facts.followups += 1
        facts.transcript.append({"role": "assistant", "content": result["ask"]})
        facts.save()
        yield {"type": "delta", "text": result["ask"], "part": "ask", "spoken": True}
        yield {"type": "facts", "phase": facts.phase}
        return
    facts.answers[q["key"]] = {"line": result["line"] or "unknown", "raw": message}
    facts.index += 1
    _advance(facts)
    facts.followups = 0
    if facts.question() is None:
        facts.phase = "done"
        facts.save()
        write_applicant(facts)
        yield {"type": "facts", "phase": facts.phase}
        return
    text = question_text(facts)
    facts.transcript.append({"role": "assistant", "content": text})
    facts.save()
    yield {"type": "break"}
    yield {"type": "delta", "text": text, "part": "ask", "spoken": True}
    yield {"type": "facts", "phase": facts.phase}


def _contact_lines(facts: Facts) -> dict:
    details = {k: v for k, v in facts.contacts.items() if k != "Email"}
    extra = facts.answers.get("contact", {}).get("line", "")
    if isinstance(extra, dict):
        details.update({k: str(v) for k, v in extra.items() if str(v).strip()})
    elif isinstance(extra, str) and extra.startswith("{"):
        try:
            details.update({k: str(v) for k, v in json.loads(extra).items() if str(v).strip()})
        except json.JSONDecodeError:
            pass
    return details


def render(facts: Facts) -> str:
    """base/applicant.md from the answers, in the shape the screen and the
    fill already read."""
    lines = []
    for label, source in FACT_LINES:
        if source == "derived":
            value = facts.derived.get(label, "unknown")
        else:
            value = facts.answers.get(source, {}).get("line") or facts.derived.get(label, "unknown")
        lines.append(f"- {label}: {value}")
    details = _contact_lines(facts)
    detail_lines = [f"- {k}: {v}" for k, v in details.items()] or ["- (nothing yet)"]
    return ("# Applicant\n\nWritten by the facts interview on the Profile tab; edit there or "
            "here.\n\n## Facts\n\n" + "\n".join(lines)
            + "\n\n## Form details\n\n" + "\n".join(detail_lines) + "\n")


def write_applicant(facts: Facts, path: Optional[Path] = None) -> Path:
    path = path or profile.APPLICANT
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(facts))
    return path


def has_applicant(path: Optional[Path] = None) -> bool:
    path = path or profile.APPLICANT
    return path.exists() and bool(profile.facts_section(path.read_text()))
