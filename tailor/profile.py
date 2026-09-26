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

import paths
from server import settings

from . import llm, prompts

ROOT = Path(__file__).resolve().parent.parent
PROFILE = paths.BASE / "profile.md"
APPLICANT = paths.BASE / "applicant.md"
STORIES = paths.BASE / "stories"
BASE_RESUME = paths.BASE / "resume.tex"
DERIVED_FACTS = paths.DATA / "derived_facts.md"

INDEX_NAME = "index.md"
TAILOR_DOC = "tailor.md"

# The picker's slots, split by whether the story is already on the resume
# (2026-09-25). One ranked list of four spent two or three of its slots on
# stories for projects and roles the resume already carries, so the tailor was
# handed nothing it could swap into PROJECTS: over the 45 runs with stories on
# file the median swap pool was two candidates and 12 runs had one or none.
# Swaps track the pool almost exactly - a pool of four produced two swaps in
# five runs of six, a pool of one produced no swap in nine of ten - so the
# pool is the fix, not the prompt. A story already on the resume is still
# worth carrying: it justifies the figures in that entry and may replace a
# bullet under the same role. It just may not crowd out the swap candidates.
SWAP_SLOTS = int(os.environ.get("AUTOPILOT_SWAP_STORIES", "3"))
DEPTH_SLOTS = int(os.environ.get("AUTOPILOT_DEPTH_STORIES", "2"))
MAX_STORIES = SWAP_SLOTS + DEPTH_SLOTS

PICK_PROMPT = """You are given a job posting and an index of the candidate's experiences,
one line per experience, starting with its slug in square brackets. Choose
the experiences whose work is closest to what the posting asks for: same
kind of system, same stack, same domain.

Some lines are marked "(already on the resume)". Those are worth choosing
when they are the closest match, but the resume already carries them, so
choose the closest experiences that are NOT marked as well: those are the
ones that can earn a place on the resume for this posting.

Reply with the chosen slugs only, one per line, most relevant first, at most
{n}. If none fit, reply with the single word NONE."""

# Same model as the screen: a short extraction, no thinking needed, and the
# tailor model's provider refuses to run with reasoning off.
def derive_model() -> str:
    return os.environ.get("OPENROUTER_SCREEN_MODEL", "deepseek/deepseek-v4-flash")


DERIVE_PROMPT = """You are given a resume in LaTeX. Write the applicant's hard facts as a
markdown bullet list, in exactly this shape, one line each, nothing else:

- Years of professional experience: <number, counted from the dated roles; internships and research posts count in full>
- Level: <entry level / mid / senior, from titles and years>
- Work authorisation: unknown
- Security clearance: unknown
- Country: <country of the most recent address or role>
- Location: <city from the resume header, or unknown>
- Relocation: acceptable anywhere in the country above; remote and hybrid are fine
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


# ---------------------------------------- what the resume already carries --

# A slug's own words are what says whether the resume already has this
# experience: `project-hydra-distributed-systems-engineering` is the master's
# "Project Hydra (Distributed Systems Engineering Platform)", and
# `auto-apply` is nowhere on it. Measured over the 15 stories on file this
# separates them exactly: the five that are on the resume score 0.75 or more,
# the ten that are not score 0.5 or less.
SLUG_MATCH = 0.6
SLUG_MIN_HITS = 2
# Words that say nothing about which experience this is.
SLUG_STOP = {"the", "and", "for", "with", "using", "project", "system", "tool",
             "app", "intern", "internship", "work"}
LINK_LINE = re.compile(r"^Link:\s*(\S+)\s*$", re.M)
STACK_LINE = re.compile(r"^Stack:\s*(.+)$", re.M)


def slug_terms(slug: str) -> list[str]:
    """The distinctive words of a slug, lowercase."""
    return [part for part in re.split(r"[-_]+", slug.lower())
            if len(part) > 2 and part not in SLUG_STOP]


def story_text(slug: str, directory: Optional[Path] = None) -> str:
    path = (directory or STORIES) / slug / TAILOR_DOC
    return path.read_text() if path.exists() else ""


def on_resume(slug: str, resume_tex: str, text: str = "") -> bool:
    """Does the master resume already carry this experience?

    Two routes, because neither alone is enough: a story's `Link:` line is
    often empty (Hydra's is) and a slug's words are not always on the page.
    """
    link = LINK_LINE.search(text or "")
    if link and link.group(1) in resume_tex:
        return True
    terms = slug_terms(slug)
    if not terms:
        return False
    from . import layout

    words = set(re.findall(r"[a-z0-9]+", layout.visible(resume_tex).lower()))
    hits = [term for term in terms if term in words]
    if len(hits) / len(terms) < SLUG_MATCH:
        return False
    # Two words agreeing is the evidence, except for a one-word slug, where
    # the one word is all there is: `gittrack` is either on the page or not.
    return len(hits) >= SLUG_MIN_HITS or len(terms) == 1


def groups(slugs: list[str], resume_tex: Optional[str] = None,
           directory: Optional[Path] = None) -> tuple[list[str], list[str]]:
    """(candidates, already on the resume), each keeping the order given.

    The candidates are what a swap can draw on; the rest are depth on what is
    already there.
    """
    directory = directory or STORIES
    if resume_tex is None:
        resume_tex = BASE_RESUME.read_text() if BASE_RESUME.exists() else ""
    candidates, already = [], []
    for slug in slugs:
        target = already if on_resume(slug, resume_tex, story_text(slug, directory)) else candidates
        target.append(slug)
    return candidates, already


def stack_overlap(text: str, posting_text: str) -> set[str]:
    """The story's stack terms that the posting names. Used to backfill a swap
    pool the picker left short, so a run is never out of stock while a story
    that shares a stack with the posting sits unused, and to decide whether a
    run that swapped nothing passed over a story it should have used."""
    haystack = posting_text.lower()
    found = set()
    for line in STACK_LINE.findall(text or ""):
        for term in re.split(r"[,;/]| and ", line):
            term = term.strip().lower()
            if len(term) < 2 or term.startswith("not "):
                continue
            if re.search(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])", haystack):
                found.add(term)
    return found


def stories(directory: Optional[Path] = None, slugs: Optional[list[str]] = None) -> str:
    """The tailor.md of each slug, concatenated. Nothing else under the
    folder ever reaches a job prompt; main.md is for the interviewer and
    star.md for the candidate."""
    directory = directory or STORIES
    resume_tex = BASE_RESUME.read_text() if BASE_RESUME.exists() else ""
    parts: list[str] = []
    for slug in slugs or []:
        path = directory / slug / TAILOR_DOC
        if not path.exists():
            continue
        text = path.read_text().strip()
        if not text:
            continue
        # Said per story, because the tailor's options differ: one that is
        # already on the resume can only deepen the entry it belongs to,
        # while one that is not is a project the posting may have earned a
        # place for. The model used to have to work this out from the master.
        note = ("already on the resume - use it to strengthen that entry or a "
                "bullet under the same role"
                if on_resume(slug, resume_tex, text) else
                "not on the resume - may take the place of a PROJECTS entry, "
                "one out and one in")
        parts.append(f"### {slug} ({note})\n\n{text}")
    return "\n\n".join(parts)


def marked_index(listing: str, resume_tex: str, directory: Optional[Path] = None) -> str:
    """The index with every line that is already on the resume said so, so
    the picker can choose the closest of each kind rather than filling its
    whole list with what the resume carries."""
    directory = directory or STORIES
    out = []
    for line in listing.splitlines():
        match = re.match(r"\s*[-*]?\s*\[([^\]]+)\]", line)
        slug = match.group(1) if match else ""
        if slug and on_resume(slug, resume_tex, story_text(slug, directory)):
            line = line.rstrip() + " (already on the resume)"
        out.append(line)
    return "\n".join(out)


def pick(posting_text: str, directory: Optional[Path] = None,
         model: Optional[str] = None, limit: int = MAX_STORIES,
         resume_tex: Optional[str] = None) -> list[str]:
    """Slugs whose story matches the posting, best first; empty when there
    is no index or the switch is off. One cheap call, done in code rather
    than as a tool call, because tool calling has already failed on a
    cheaper model once.

    The slots are split (2026-09-25): at most `SWAP_SLOTS` stories that the
    resume does not already carry and at most `DEPTH_SLOTS` that it does, both
    in the picker's own order. A pool left short of swap candidates is filled
    from the stories the picker passed over, best stack overlap with the
    posting first, so a run is never out of stock while a story that shares a
    stack with the posting sits unused. Candidates come first in the returned
    list: that is the order the tailor reads them in.
    """
    directory = directory or STORIES
    if not settings.use_profile():
        return []
    listing = index(directory)
    if not listing:
        return []
    if resume_tex is None:
        resume_tex = BASE_RESUME.read_text() if BASE_RESUME.exists() else ""
    known = [p.name for p in story_dirs(directory)]
    reply = llm.complete(
        prompts.text("profile.pick").replace("{n}", str(limit)),
        f"## Index\n\n{marked_index(listing, resume_tex, directory)}"
        f"\n\n## Posting\n\n{posting_text[:12_000]}",
        model=model or derive_model(), temperature=0.0, max_tokens=400,
        reasoning={"enabled": False},
    )
    chosen: list[str] = []
    for line in reply.splitlines():
        slug = line.strip().strip("-*[] ").split()[0] if line.strip() else ""
        slug = slug.strip("[]:,")
        if slug in known and slug not in chosen:
            chosen.append(slug)

    candidates, already = groups(chosen, resume_tex, directory)
    swap_slots = max(0, min(SWAP_SLOTS, limit))
    if len(candidates) < swap_slots:
        spare = [slug for slug in known if slug not in chosen]
        ranked = sorted(
            groups(spare, resume_tex, directory)[0],
            key=lambda slug: -len(stack_overlap(story_text(slug, directory), posting_text)),
        )
        for slug in ranked:
            if len(candidates) >= swap_slots:
                break
            if stack_overlap(story_text(slug, directory), posting_text):
                candidates.append(slug)
    depth_slots = max(0, min(DEPTH_SLOTS, limit - len(candidates[:swap_slots])))
    return (candidates[:swap_slots] + already[:depth_slots])[:limit]


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
        prompts.text("profile.derive"), resume_path.read_text(), model=model or derive_model(),
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
