"""What the models know about the applicant, and where it comes from.

Three layers, each optional:

- base/profile.md: the free-text profile the tailor has always read.
- base/applicant.md: hard facts (years, visa, dates) and form details.
- base/stories/<slug>/: one folder per role or project, written by the
  Phase 7 interviewer. main.md is the candidate's words; tailor.md is the
  dense derivative the job prompts read. index.md, one line per slug, is
  always in the prompt; a cheap call picks the few tailor.md files that
  match a posting, so the prompt never carries every story.

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

INDEX_NAME = "index.md"
TAILOR_DOC = "tailor.md"
# The picker returns this many at most. Three or four stories is what a
# posting can use; every one past that is prompt the tailor drifts on.
MAX_STORIES = 4

PICK_PROMPT = """You are given a job posting and an index of the candidate's experiences,
one line per experience, starting with its slug in square brackets. Choose
the experiences whose work is closest to what the posting asks for: same
kind of system, same stack, same domain. Reply with the chosen slugs only,
one per line, most relevant first, at most {n}. If none fit, reply with the
single word NONE."""

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


def story_dirs(directory: Optional[Path] = None) -> list[Path]:
    """Every experience folder, alphabetical. Folders starting with an
    underscore are the interviewer's own state, not experiences."""
    directory = directory or STORIES
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.iterdir()
                  if p.is_dir() and not p.name.startswith((".", "_")))


def index(directory: Optional[Path] = None) -> str:
    """base/stories/index.md, or empty. Regenerated with the children."""
    directory = directory or STORIES
    path = directory / INDEX_NAME
    return path.read_text().strip() if path.exists() else ""


def stories(directory: Optional[Path] = None, slugs: Optional[list[str]] = None) -> str:
    """The tailor.md of each slug, concatenated. Nothing else under the
    folder ever reaches a job prompt; main.md is for the interviewer and
    star.md for the candidate."""
    directory = directory or STORIES
    parts: list[str] = []
    for slug in slugs or []:
        path = directory / slug / TAILOR_DOC
        if not path.exists():
            continue
        text = path.read_text().strip()
        if text:
            parts.append(f"### {slug}\n\n{text}")
    return "\n\n".join(parts)


def pick(posting_text: str, directory: Optional[Path] = None,
         model: Optional[str] = None, limit: int = MAX_STORIES) -> list[str]:
    """Slugs whose story matches the posting, best first; empty when there
    is no index or the switch is off. One cheap call, done in code rather
    than as a tool call, because tool calling has already failed on a
    cheaper model once."""
    directory = directory or STORIES
    if not settings.use_profile():
        return []
    listing = index(directory)
    if not listing:
        return []
    known = {p.name for p in story_dirs(directory)}
    reply = llm.complete(
        PICK_PROMPT.format(n=limit),
        f"## Index\n\n{listing}\n\n## Posting\n\n{posting_text[:12_000]}",
        model=model or derive_model(), temperature=0.0, max_tokens=400,
        reasoning={"enabled": False},
    )
    chosen: list[str] = []
    for line in reply.splitlines():
        slug = line.strip().strip("-*[] ").split()[0] if line.strip() else ""
        slug = slug.strip("[]:,")
        if slug in known and slug not in chosen:
            chosen.append(slug)
    return chosen[:limit]


def read_used(app_dir: Optional[Path]) -> list[str]:
    """The slugs the pipeline picked for this application, so the cover
    letter and form answers see the same stories the resume did."""
    if app_dir is None:
        return []
    path = Path(app_dir) / "stories_used.txt"
    if not path.exists():
        return []
    return [line.strip() for line in path.read_text().splitlines() if line.strip()]


def context(base_profile: Optional[str] = None, slugs: Optional[list[str]] = None) -> str:
    """profile.md, plus applicant facts, the story index, and the picked
    stories when the switch is on.

    This is what tailor, cover, and answers receive as `profile`. With the
    switch off it is profile.md alone: the behaviour before the profile.
    `slugs` is the pipeline's pick for this posting; without one the index
    still goes in, so the model knows what exists, but no story does.
    """
    text = load_profile() if base_profile is None else base_profile
    if not settings.use_profile():
        return text
    facts = applicant_facts()
    listing = index()
    extra = stories(slugs=slugs)
    if facts:
        text = f"{text}\n\n## Applicant facts\n\n{facts}".strip()
    if listing:
        text = f"{text}\n\n## Experiences on file (one line each)\n\n{listing}".strip()
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
