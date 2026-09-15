"""What the models know about the applicant, and where it comes from.

Three layers, each optional:

- base/profile.md: the free-text profile the tailor has always read.
- base/applicant.md: hard facts (years, visa, dates) and form details.
- base/stories/*.md: one document per role or project, written by the
  Phase 7 interviewer. Full context for swapping a project or rewriting a
  bullet around a fact.

The "Use profile" switch on the review page gates the last two. Off, or on
with nothing written yet, and every caller falls back to what it had before
the profile existed: the resume and profile.md. The screen's fallback is a
set of facts derived once from the resume, cached until the resume changes.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

from server import settings

from . import llm

ROOT = Path(__file__).resolve().parent.parent
PROFILE = ROOT / "base" / "profile.md"
APPLICANT = ROOT / "base" / "applicant.md"
STORIES = ROOT / "base" / "stories"
BASE_RESUME = ROOT / "base" / "resume.tex"
DERIVED_FACTS = ROOT / "data" / "derived_facts.md"

# Stories are appended whole. Past this many characters the prompt is
# mostly stories, and the tailor starts drifting from the posting.
STORIES_CHAR_CAP = 24_000

# Same model as the screen: a short extraction, no thinking needed, and the
# tailor model's provider refuses to run with reasoning off.
def derive_model() -> str:
    return os.environ.get("OPENROUTER_SCREEN_MODEL", "deepseek/deepseek-v4-flash")


DERIVE_PROMPT = """You are given a resume in LaTeX. Write the applicant's hard facts as a
markdown bullet list, in exactly this shape, one line each, nothing else:

- Years of professional experience: <number, counted from the dated roles; internships count at half>
- Level: <entry level / mid / senior, from titles and years>
- Work authorisation: unknown
- Security clearance: unknown
- Country: <country of the most recent address or role>
- Location: <city from the resume header, or unknown>
- Earliest start date: unknown
- Degrees held: <each degree with its year>
- Graduation: <most recent graduation year, and whether it is in the past>

Use only what the resume states. Where it says nothing, write "unknown". Do
not add lines, prose, or headings."""


def load_profile(path: Optional[Path] = None) -> str:
    path = path or PROFILE
    return path.read_text() if path.exists() else ""


def facts_section(text: str) -> Optional[str]:
    match = re.search(r"^## Facts\s*\n(.*?)(?=^## |\Z)", text, re.S | re.M)
    if not match or not match.group(1).strip():
        return None
    return match.group(1).strip()


def applicant_facts(path: Optional[Path] = None) -> Optional[str]:
    """The Facts section of base/applicant.md, or None when absent."""
    path = path or APPLICANT
    if not path.exists():
        return None
    return facts_section(path.read_text())


def stories(directory: Optional[Path] = None, cap: int = STORIES_CHAR_CAP) -> str:
    """Every story document, concatenated, alphabetical, capped. Empty if none."""
    directory = directory or STORIES
    if not directory.is_dir():
        return ""
    parts: list[str] = []
    used = 0
    for path in sorted(directory.glob("*.md")):
        text = path.read_text().strip()
        if not text:
            continue
        block = f"### {path.stem}\n\n{text}"
        if used + len(block) > cap:
            break
        parts.append(block)
        used += len(block)
    return "\n\n".join(parts)


def context(base_profile: Optional[str] = None) -> str:
    """profile.md, plus applicant facts and stories when the switch is on.

    This is what tailor, cover, and answers receive as `profile`. With the
    switch off it is profile.md alone: the behaviour before the profile.
    """
    text = load_profile() if base_profile is None else base_profile
    if not settings.use_profile():
        return text
    facts = applicant_facts()
    extra = stories()
    if facts:
        text = f"{text}\n\n## Applicant facts\n\n{facts}".strip()
    if extra:
        text = f"{text}\n\n## Experience in detail\n\n{extra}".strip()
    return text


def derived_facts(resume_path: Optional[Path] = None, cache: Optional[Path] = None,
                  model: Optional[str] = None) -> str:
    """Facts inferred from the resume, cached until the resume changes.

    A model call per page open would make the banner slow and cost money for
    the same answer every time, so the derivation runs once and the cache
    carries the resume's mtime in its first line.
    """
    resume_path = resume_path or BASE_RESUME
    cache = cache or DERIVED_FACTS
    if not resume_path.exists():
        raise FileNotFoundError(f"{resume_path} is missing; nothing to derive facts from")
    stamp = f"<!-- resume mtime {int(resume_path.stat().st_mtime)} -->"
    if cache.exists():
        cached = cache.read_text()
        if cached.startswith(stamp):
            return cached.split("\n", 1)[1].strip()
    reply = llm.complete(
        DERIVE_PROMPT, resume_path.read_text(), model=model or derive_model(),
        temperature=0.0, max_tokens=2000, reasoning={"enabled": False},
    )
    facts = "\n".join(line for line in reply.splitlines() if line.startswith("- ")).strip()
    if not facts:
        raise RuntimeError("could not derive facts from the resume")
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(f"{stamp}\n{facts}\n")
    return facts


def screen_facts() -> tuple[str, str]:
    """(facts, source) for the screen. Source is 'applicant.md' or 'resume'."""
    if settings.use_profile():
        facts = applicant_facts()
        if facts:
            return facts, "applicant.md"
    return derived_facts(), "resume"
