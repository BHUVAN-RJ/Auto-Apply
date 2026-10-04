"""The cover letter step: fill the person's standing letter, then make a PDF.

The letter is a template with named slots. Code fills the role and company
from the posting. The model writes only the company-specific and
role-specific slots. The greeting, the standing paragraphs and the sign-off
are the person's and are never rewritten.

The rules live in cover_rules.md and go to the model verbatim. This file
parses the template, checks the fills, assembles the letter and turns it
into a one-page PDF through the same LaTeX path the resume uses.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import llm, prompts
from .fetch import Posting

RULES = Path(__file__).resolve().parent / "cover_rules.md"
EXAMPLE = Path(__file__).resolve().parent.parent / "base" / "cover.example.md"

REPLY_FORMAT = """
## Reply format

Reply with the slot fills only. One block per slot, in this form:

OPENING:
two sentences

ROLE_SPECIFIC_EVIDENCE:
two or three sentences

COMPANY_SPECIFIC_PARAGRAPH:
the paragraph

SPECIFIC_GOAL_OR_TEAM:
a short phrase

Use the slot names you were given. No greeting, no sign-off, no letter
around the slots, no preamble, no commentary, no code fence, no markdown.
"""

# A faithful fill of a traditional standing letter lands well above the old
# 260-word cap. The floor still catches a note; the ceiling catches a
# rewritten resume. Slot checks do the rest.
MIN_WORDS, MAX_WORDS = 120, 420
ATTEMPTS = 3

# Dash characters the model reaches for as punctuation. The rules forbid
# them, the reply is rejected when they appear, and if they survive every
# retry they are rewritten as commas rather than shipped.
DASHES = ("\u2014", "\u2013", "\u2012", "\u2015")

# Filled from the posting. The model is not asked for these.
CODE_SLOTS = frozenset({"JOB_ROLE", "COMPANY_NAME"})

# Opening and the company paragraph have to be about this employer.
COMPANY_SLOTS = frozenset({"OPENING", "COMPANY_SPECIFIC_PARAGRAPH"})

# Per-slot ceilings. SPECIFIC_GOAL_OR_TEAM is a phrase, not sentences.
SLOT_SENTENCES = {
    "OPENING": 2,
    "ROLE_SPECIFIC_EVIDENCE": 3,
    "COMPANY_SPECIFIC_PARAGRAPH": 5,
}
SLOT_WORDS = {
    "OPENING": 70,
    "ROLE_SPECIFIC_EVIDENCE": 90,
    "COMPANY_SPECIFIC_PARAGRAPH": 90,
    "SPECIFIC_GOAL_OR_TEAM": 12,
}

BANNED = (
    "i am excited to apply",
    "i am applying for",
    "passionate about",
    "fast-paced",
    "leverage",
    "align with",
    "resonate",
    "great fit",
)

SLOT = re.compile(r"<([A-Z][A-Z0-9_]*)(?:\s+([^>]+))?>")
BLOCK = re.compile(
    r"^([A-Z][A-Z0-9_]+):\s*(.*?)(?=^[A-Z][A-Z0-9_]+:\s*|\Z)",
    re.M | re.S,
)


@dataclass
class Slot:
    name: str
    instruction: str


@dataclass
class CoverLetter:
    text: str
    model: str
    attempts: int = 1

    @property
    def words(self) -> int:
        return len(self.text.split())


class CoverLetterError(RuntimeError):
    pass


def system_prompt() -> str:
    return prompts.text("cover") + "\n" + prompts.text("cover.format")


def template_path() -> Path:
    """The person's standing letter, resolved per call so a test can move it."""
    import paths
    return paths.BASE / "cover.md"


def load_template(path: Optional[Path] = None) -> str:
    """The person's letter if they wrote one, else the stock example."""
    chosen = path or template_path()
    if chosen.is_file():
        return chosen.read_text()
    if EXAMPLE.is_file():
        return EXAMPLE.read_text()
    raise CoverLetterError(
        "no cover letter template; copy base/cover.example.md to base/cover.md"
    )


def parse_slots(template: str) -> list[Slot]:
    """Named placeholders in document order. A slot is <NAME instruction>."""
    seen: list[Slot] = []
    names: set[str] = set()
    for match in SLOT.finditer(template):
        name = match.group(1)
        if name in names:
            continue
        names.add(name)
        seen.append(Slot(name=name, instruction=(match.group(2) or "").strip()))
    return seen


def model_slots(template: str) -> list[Slot]:
    return [slot for slot in parse_slots(template) if slot.name not in CODE_SLOTS]


def parse_fills(reply: str) -> dict[str, str]:
    """Labeled blocks from the model. Unknown labels are ignored."""
    return {
        match.group(1): match.group(2).strip()
        for match in BLOCK.finditer(clean(reply))
    }


def code_value(name: str, posting: Posting) -> str:
    if name == "JOB_ROLE":
        return (posting.title or "").strip() or "this"
    if name == "COMPANY_NAME":
        return (posting.company or "").strip() or "the company"
    raise CoverLetterError(f"not a code-filled slot: {name}")


def assemble(template: str, fills: dict[str, str], posting: Posting) -> str:
    """Standing prose plus the fills. Code slots come from the posting."""

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in CODE_SLOTS:
            return code_value(name, posting)
        return (fills.get(name) or "").strip()

    return SLOT.sub(replace, template).strip() + "\n"


def _user_message(posting: Posting, resume_tex: str, profile: str, extra: str,
                  slots: list[Slot]) -> str:
    sections = [f"## Job posting\n\n{posting.to_markdown()}"]
    if profile:
        sections.append("## Candidate profile\n\n" + profile)
    sections.append(f"## The tailored resume, as LaTeX\n\n```tex\n{resume_tex}\n```")
    role = code_value("JOB_ROLE", posting)
    company = code_value("COMPANY_NAME", posting)
    sections.append(
        f"## The posting's own names\n\nRole: {role}\nCompany: {company}"
    )
    lines = []
    for slot in slots:
        note = f" {slot.instruction}" if slot.instruction else ""
        lines.append(f"### {slot.name}\n{note.strip()}")
    sections.append("## Slots to fill\n\n" + "\n\n".join(lines))
    if extra.strip():
        sections.append(
            "## Additional instruction from the reviewer\n\n"
            f"{extra.strip()}\n\nApply this while still obeying every rule above."
        )
    return "\n\n".join(sections)


def clean(reply: str) -> str:
    """Strip the wrappers a model adds despite being told not to."""
    text = reply.strip()
    fence = re.match(r"^```[a-zA-Z]*\n(.*?)\n```$", text, re.S)
    if fence:
        text = fence.group(1).strip()
    # Collapse runs of blank lines; paragraphs are separated by exactly one.
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def sentence_count(text: str) -> int:
    """Count sentences without treating 9.1 as a full stop."""
    cleaned = re.sub(r"\d+\.\d+", "0", text.strip())
    if not cleaned:
        return 0
    return len(re.findall(r"[.!?]+", cleaned))


def mentions_company(text: str, posting: Posting) -> bool:
    """The slot talks about this employer, not employers in general."""
    haystack = text.lower()
    company = (posting.company or "").strip()
    if company:
        if company.lower() in haystack:
            return True
        tokens = [tok for tok in re.findall(r"[A-Za-z][A-Za-z0-9]+", company) if len(tok) >= 4]
        return any(tok.lower() in haystack for tok in tokens)
    # No company on the posting: a named product or team from the title is
    # the next best proof the slot is about this job.
    title = (posting.title or "").strip()
    return any(
        len(tok) >= 5 and tok.lower() in haystack
        for tok in re.findall(r"[A-Za-z][A-Za-z0-9]+", title)
    )


def _invented(text: str, resume_tex: str, profile: str, posting: Posting) -> list[str]:
    """Figures and names the resume, profile and posting cannot support."""
    from . import layout
    from . import tailor

    on_file = layout.visible(resume_tex) + "\n" + (profile or "")
    found = []
    invented = sorted(tailor._numbers(text) - tailor._numbers(on_file))
    if invented:
        found.append(
            "it states figures that are on neither the resume nor the profile: "
            + ", ".join(invented)
        )
    # Company and title are part of the posting even when the body never
    # repeats them. A slot that names Example Corp is using the posting,
    # not inventing an employer.
    posting_blob = "\n".join(
        part for part in (posting.company, posting.title, posting.text) if part
    )
    known = tailor._words(on_file) | tailor._words(posting_blob)
    strange = sorted(
        word for stem, word in tailor._named(text).items()
        if not tailor._is_known(stem, known)
    )
    if strange:
        found.append(
            "it names things that are on neither the resume, the profile, "
            "nor the posting: " + ", ".join(strange)
        )
    return found


def slot_problems(fills: dict[str, str], slots: list[Slot], posting: Posting,
                  resume_tex: str = "", profile: str = "") -> list[str]:
    """Why the fills cannot go into the letter. Empty means they can."""
    found = []
    wanted = [slot.name for slot in slots]
    for name in wanted:
        text = (fills.get(name) or "").strip()
        if not text:
            found.append(f"{name} was missing")
            continue
        if name == "SPECIFIC_GOAL_OR_TEAM" and sentence_count(text) > 1:
            found.append(f"{name} must be a short phrase, not a paragraph")
        limit = SLOT_SENTENCES.get(name)
        if limit is not None and sentence_count(text) > limit:
            found.append(
                f"{name} was {sentence_count(text)} sentences; write at most {limit}"
            )
        words = len(text.split())
        word_cap = SLOT_WORDS.get(name)
        if word_cap is not None and words > word_cap:
            found.append(f"{name} was {words} words; keep it under {word_cap}")
        lowered = text.lower()
        for phrase in BANNED:
            if phrase in lowered:
                found.append(f"{name} used {phrase!r}; say something specific instead")
                break
        if name in COMPANY_SLOTS and posting.company and not mentions_company(text, posting):
            found.append(
                f"{name} did not mention {posting.company} or something it is building"
            )
        if has_dash(text):
            found.append(
                f"{name} used a dash as punctuation; use a comma or a full stop instead"
            )
        found.extend(
            f"{name}: {reason}"
            for reason in _invented(text, resume_tex, profile, posting)
        )
    extra = sorted(name for name in fills if name not in wanted and name not in CODE_SLOTS)
    if extra:
        found.append("it filled slots that were not asked for: " + ", ".join(extra))
    return found


def problems(text: str) -> list[str]:
    """Why the assembled letter is not acceptable. Empty means it is."""
    found = []
    words = len(text.split())
    if words < MIN_WORDS or words > MAX_WORDS:
        found.append(f"it was {words} words; write between {MIN_WORDS} and {MAX_WORDS}")
    if SLOT.search(text):
        found.append("unfilled slots were left in the letter")
    if has_dash(text):
        found.append("it used a dash as punctuation; use a comma or a full stop instead")
    return found


def has_dash(text: str) -> bool:
    if any(dash in text for dash in DASHES):
        return True
    # A spaced hyphen is the same habit in ASCII: "the role - which".
    return bool(re.search(r"\s-\s|\s--\s", text))


def strip_dashes(text: str) -> str:
    """Last resort: dashes as commas. Spacing is tidied so ", ," never appears."""
    out = text
    for dash in DASHES:
        out = out.replace(dash, ",")
    out = re.sub(r"[ \t]+--?[ \t]+", ", ", out)
    out = re.sub(r"[ \t]*,[ \t]*", ", ", out)          # one space after, none before
    out = re.sub(r"(, )+", ", ", out)                    # ", , " from two dashes
    out = re.sub(r", ([.!?,;])", r"\1", out)             # ", ." -> "."
    return re.sub(r", (?=\n|$)", "", out)                # no dangling comma at a line end


def _only_dashes(found: list[str]) -> bool:
    return bool(found) and all("dash as punctuation" in item for item in found)


def write(posting: Posting, resume_tex: str, profile: str = "", extra_instruction: str = "",
          model: Optional[str] = None, template: Optional[str] = None) -> CoverLetter:
    """Ask for the slots, again while a fill breaks a rule that is checkable.

    The letter follows the resume's model: a job tailored with Opus gets its
    slots from Opus too, so the two documents a human reads sound alike."""
    source = template if template is not None else load_template()
    slots = model_slots(source)
    if not slots:
        raise CoverLetterError("cover letter template has no slots to fill")
    system = system_prompt()
    user = _user_message(posting, resume_tex, profile, extra_instruction, slots)
    model = model or llm.tailor_model()
    last_fills: dict[str, str] = {}
    last_problems: list[str] = []
    for attempt in range(1, ATTEMPTS + 1):
        reply = llm.complete(system, user, model=model, temperature=0.5, max_tokens=4000)
        fills = parse_fills(reply)
        last_fills, last_problems = fills, slot_problems(
            fills, slots, posting, resume_tex, profile
        )
        if not last_problems:
            letter = assemble(source, fills, posting)
            assembled = problems(letter)
            if not assembled:
                return CoverLetter(text=letter, model=model, attempts=attempt)
            last_problems = assembled
        user += (
            "\n\n## Your previous reply was rejected\n\n"
            + "; ".join(last_problems)
            + ". Write the slots again, obeying every rule above."
        )
    if _only_dashes(last_problems):
        cleaned = {name: strip_dashes(text) for name, text in last_fills.items()}
        letter = assemble(source, cleaned, posting)
        leftover = [item for item in problems(letter) if "dash as punctuation" not in item]
        if not leftover:
            return CoverLetter(text=strip_dashes(letter), model=model, attempts=ATTEMPTS)
        last_problems = leftover
    raise CoverLetterError(
        f"cover letter broke the rules on {ATTEMPTS} attempts: " + "; ".join(last_problems)
    )


# --- PDF ---------------------------------------------------------------------

# Deliberately plain: pdflatex, packages BasicTeX already ships, the same font
# family and paper as the resume template. Nothing here to go missing.
TEMPLATE = r"""\documentclass[letterpaper,11pt]{article}
\usepackage[T1]{fontenc}
\usepackage[utf8]{inputenc}
\usepackage[margin=1in]{geometry}
\usepackage{parskip}
\usepackage[hidelinks]{hyperref}
\pagestyle{empty}
\begin{document}

%(date)s

\vspace{1.5em}

%(body)s

\end{document}
"""

_SPECIALS = {
    "\\": r"\textbackslash{}",
    "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_",
    "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}",
}


_SPECIALS_RE = re.compile("[" + re.escape("".join(_SPECIALS)) + "]")


def escape(text: str) -> str:
    """Escape LaTeX specials in prose, in one pass so no replacement is re-escaped."""
    return _SPECIALS_RE.sub(lambda m: _SPECIALS[m.group(0)], text)


def to_tex(text: str, today: dt.date | None = None) -> str:
    """The letter as a LaTeX document.

    Paragraphs are blank-line separated in the text and stay so; the greeting
    and the sign-off block get a line break after each line rather than a
    paragraph, so "Best," sits directly over the name.
    """
    today = today or dt.date.today()
    paragraphs = [p.strip() for p in text.strip().split("\n\n") if p.strip()]
    rendered = []
    for paragraph in paragraphs:
        lines = [escape(line.strip()) for line in paragraph.splitlines() if line.strip()]
        rendered.append(" \\\\\n".join(lines) if len(lines) > 1 else lines[0])
    return TEMPLATE % {
        "date": escape(today.strftime("%B %-d, %Y")),
        "body": "\n\n".join(rendered),
    }
