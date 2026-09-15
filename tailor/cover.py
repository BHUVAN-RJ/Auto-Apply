"""The cover letter step: one short letter per posting, as text and as a PDF.

Runs after the resume is tailored, from the tailored resume rather than the
master, so the letter leads with the same things the resume now leads with.
The rules live in cover_rules.md and go to the model verbatim; this file only
formats the reply and turns it into a one-page PDF through the same LaTeX
path the resume uses.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from pathlib import Path

from . import llm
from .fetch import Posting

RULES = Path(__file__).resolve().parent / "cover_rules.md"

REPLY_FORMAT = """
## Reply format

Reply with the letter only: greeting, paragraphs, "Sincerely," and the name.
No preamble, no commentary, no code fence, no markdown.
"""

# Word bounds, checked rather than trusted. A letter far outside them is
# either a rewrite of the resume or a note, and neither is worth a retry
# budget: it is rejected once with the count, and the model gets one more go.
MIN_WORDS, MAX_WORDS = 100, 260
ATTEMPTS = 3

# Dash characters the model reaches for as punctuation. The rules forbid
# them, the reply is rejected when they appear, and if they survive every
# retry they are rewritten as commas rather than shipped.
DASHES = ("\u2014", "\u2013", "\u2012", "\u2015")


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
    return RULES.read_text() + "\n" + REPLY_FORMAT


def _user_message(posting: Posting, resume_tex: str, profile: str, extra: str) -> str:
    sections = [f"## Job posting\n\n{posting.to_markdown()}"]
    if profile:
        sections.append("## Candidate profile\n\n" + profile)
    sections.append(f"## The tailored resume, as LaTeX\n\n```tex\n{resume_tex}\n```")
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


def problems(text: str) -> list[str]:
    """Why a reply is not acceptable as written. Empty means it is."""
    found = []
    words = len(text.split())
    if words < MIN_WORDS or words > MAX_WORDS:
        found.append(f"it was {words} words; write between {MIN_WORDS} and {MAX_WORDS}")
    if "sincerely" not in text.lower():
        found.append('it did not end with "Sincerely," and the name')
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


def write(posting: Posting, resume_tex: str, profile: str = "", extra_instruction: str = "") -> CoverLetter:
    """Ask for the letter, again while it breaks a rule that is checkable."""
    system = system_prompt()
    user = _user_message(posting, resume_tex, profile, extra_instruction)
    model = llm.tailor_model()
    last, last_problems = "", []
    for attempt in range(1, ATTEMPTS + 1):
        reply = clean(llm.complete(system, user, model=model, temperature=0.5, max_tokens=4000))
        last, last_problems = reply, problems(reply)
        if not last_problems:
            return CoverLetter(text=reply, model=model, attempts=attempt)
        user += (
            "\n\n## Your previous reply was rejected\n\n"
            + "; ".join(last_problems)
            + ". Write it again, obeying every rule above."
        )
    # Only a dash problem can be repaired without the model; anything else
    # means the letter is the wrong thing entirely.
    if last_problems == ["it used a dash as punctuation; use a comma or a full stop instead"]:
        return CoverLetter(text=strip_dashes(last), model=model, attempts=ATTEMPTS)
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
    paragraph, so "Sincerely," sits directly over the name.
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
