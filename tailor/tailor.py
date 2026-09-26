"""Resume tailoring.

Takes the posting text plus the master resume source, and returns a rewritten
resume with a written rationale for each change. The model is asked for fenced
blocks rather than JSON, because LaTeX is full of backslashes and braces that
JSON escaping mangles in practice.

Two things are enforced in code rather than left to the prompt, because prompt
rules leak: the resume must still compile to its original page count, and the
tailored source must not have touched any section outside the allowed set.
"""

from __future__ import annotations

import difflib
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import paths

from . import layout, llm, profile as profile_module, prompts, structure
from .fetch import Posting

ROOT = Path(__file__).resolve().parent.parent
BASE_RESUME = paths.BASE / "resume.tex"
PROFILE = paths.BASE / "profile.md"
RULES = Path(__file__).resolve().parent / "rules.md"

# Sections the model is allowed to rewrite. Anything else must come back
# byte-identical, and _check_frozen_sections verifies that.
EDITABLE_SECTIONS = ("SUMMARY", "EXPERIENCE", "PROJECTS", "TECHNICAL SKILLS")

# What has to hold is the page, and what fills a page is lines, not
# characters (2026-09-24). A bullet may be rewritten with more words or
# fewer as long as it still prints on the same number of lines; the line
# width is measured off the compiled master in `layout.py`. Growing by a
# line rejects the attempt, since that is what pushes the resume to two
# pages; coming back a line shorter is only a warning.

REPLY_FORMAT = """
Reply in exactly this shape and nothing else.

If the posting is a clear mismatch, reply with only:

```verdict
MISMATCH: <one sentence saying why>
```

Otherwise reply with three blocks:

```verdict
MATCH: <one sentence on why this candidate fits>
```

```tex
<the complete tailored LaTeX source, whole file, preamble included>
```

```markdown
<one line starting "Asks:" with the posting's top five terms, comma separated.
Then one bullet per change, twelve words at most, `where: "old" -> "new" (why)`.
Then one line starting "Gaps:" listing posting requirements the candidate
genuinely does not meet, comma separated.>
```
"""


@dataclass
class TailorResult:
    tex: str
    suggestions: str
    diff: str
    model: str
    verdict: str = ""
    attempts: int = 1
    warnings: list[str] = field(default_factory=list)
    # Why each earlier attempt was rejected, oldest first. Written to the
    # rationale: a resume that came back barely changed is usually a
    # checker the model kept hitting, and that was invisible before.
    rejections: list[str] = field(default_factory=list)


class TailorError(RuntimeError):
    """A tailoring pass that could not be accepted.

    Carries every rejected candidate so the failure can be inspected after the
    fact. Without this the only evidence of what went wrong is a one-line
    reason, and a rejection that only happens on the third retry is otherwise
    impossible to reproduce.
    """

    def __init__(self, message: str, attempts: Optional[list[dict]] = None) -> None:
        super().__init__(message)
        self.attempts = attempts or []


class Mismatch(TailorError):
    """The model judged this posting a poor fit. Nothing was tailored."""


def system_prompt() -> str:
    return prompts.text("tailor") + "\n" + prompts.text("tailor.format")


def _extract_block(text: str, languages: tuple[str, ...]) -> Optional[str]:
    """Pull the first fenced block whose info string matches one of `languages`."""
    for language in languages:
        match = re.search(rf"```{language}[^\n]*\n(.*?)```", text, re.S)
        if match:
            return match.group(1).strip("\n")
    return None


def make_diff(before: str, after: str, path: str = "resume.tex") -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=3,
        )
    )


def split_sections(tex: str) -> dict[str, str]:
    """Map section name to its body, in whatever layout the resume is.

    The four sections the rules name (SUMMARY, EXPERIENCE, PROJECTS,
    TECHNICAL SKILLS) are keyed by those names whatever the resume calls
    them ("Work Experience", "Profile"); every other section by its own
    heading. Everything before the first heading is `PREAMBLE`.
    `tailor/structure.py` reads the layout.
    """
    return structure.sections(tex)


def master_problems(tex: str) -> list[str]:
    """Why this master resume cannot be tailored, or nothing.

    Any LaTeX layout will do: the person keeps their own design. What the
    tailor needs is a section it can recognise as the experience, with
    bullets under it; the summary, projects and skills are tailored when
    they are there and simply left alone when they are not.
    """
    found = structure.titles(tex)
    if "EXPERIENCE" not in found:
        named = ", ".join(f'"{t}"' for t in found.values()) or "none"
        return [f"no section reads as the work experience (headings found: {named}); "
                "a heading such as Experience or Work Experience is needed"]
    if not bullets(split_sections(tex)["EXPERIENCE"]):
        return ["no bullets under the experience section (\\item or \\resumeItem)"]
    return []


def restore_preamble(original: str, tailored: str) -> tuple[str, bool]:
    """Put the master's preamble back on a tailored resume.

    Nothing above `\\begin{document}` may change, and the check is byte
    exact — but a model that reflows one comment or drops a space inside a
    macro loses the whole reply, and with four attempts that is a quarter
    of the budget for something no reader would see. Abridge spent two of
    four attempts there. So the preamble is not argued about: it is
    replaced by the master's, and the reply is judged on the sections,
    which is where tailoring lives.

    Returns the spliced resume and whether anything had to be restored.
    """
    here, there = structure.first_heading(original), structure.first_heading(tailored)
    if here is None or there is None:
        return tailored, False
    if _normalise(original[:here]) == _normalise(tailored[:there]):
        return tailored, False
    return original[:here] + tailored[there:], True


def _check_frozen_sections(original: str, tailored: str) -> None:
    """Every section outside EDITABLE_SECTIONS must be untouched."""
    before, after = split_sections(original), split_sections(tailored)

    missing = set(before) - set(after)
    if missing:
        raise TailorError(f"tailored resume dropped section(s): {', '.join(sorted(missing))}")
    added = set(after) - set(before)
    if added:
        raise TailorError(f"tailored resume added section(s): {', '.join(sorted(added))}")

    for name, body in before.items():
        if name in EDITABLE_SECTIONS:
            continue
        if _normalise(after[name]) != _normalise(body):
            raise TailorError(
                f"tailored resume modified {name}, which is not an editable section"
            )


def _normalise(text: str) -> str:
    """Collapse whitespace so a reflowed line is not read as a content change."""
    return re.sub(r"\s+", " ", text).strip()


def _balanced_body(tex: str, open_index: int) -> Optional[str]:
    """Text between the brace at `open_index` and its matching close brace."""
    return structure.balanced(tex, open_index)


def bullets(tex: str) -> list[str]:
    """Every bullet body, in document order: `\\resumeItem{...}` when the
    resume uses it, else each `\\item`. Bodies contain nested braces from
    \\normalsize, \\href and \\textbf, so they are read by counting depth
    (`tailor/structure.py`), not by a regex."""
    return structure.bullets(tex)


def bullet_lengths(tex: str) -> list[int]:
    """Character length of every \\resumeItem body, in document order."""
    return [len(_normalise(body)) for body in bullets(tex)]


def bullet_budgets(tex: str) -> list[tuple[int, int, int]]:
    """Per bullet: (printed lines, characters that fit, characters used)."""
    width = layout.chars_per_line()
    return [layout.budget(body, width) for body in bullets(tex)]


def pair_bullets(original: str, tailored: str) -> list[tuple[int, int, int]]:
    """Match each tailored bullet to the master bullet it came from.

    Returns (master index from 1, master length, tailored length). Matched by
    text similarity rather than position, because the rules allow reordering
    projects and entries: with positional matching a reorder read as one
    bullet growing by 135 characters and another shrinking by the same, and
    the retry feedback told the model to cut a bullet that had not changed.
    """
    import difflib

    before = [_normalise(b) for b in bullets(original)]
    after = [_normalise(b) for b in bullets(tailored)]
    if len(before) != len(after):
        raise TailorError(
            f"bullet count changed ({len(before)} -> {len(after)}); "
            "the layout is tuned for exactly this many"
        )
    scores = sorted(
        ((difflib.SequenceMatcher(None, a, b).ratio(), i, j)
         for i, a in enumerate(before) for j, b in enumerate(after)),
        reverse=True,
    )
    taken_before: set[int] = set()
    taken_after: set[int] = set()
    pairs = []
    for _, i, j in scores:
        if i in taken_before or j in taken_after:
            continue
        taken_before.add(i)
        taken_after.add(j)
        pairs.append((i + 1, len(before[i]), len(after[j])))
        if len(pairs) == len(before):
            break
    return sorted(pairs)


# PROJECTS is measured as a block, not entry by entry (2026-09-24): a
# story swapped in may be worth more lines than the entry it replaced and
# another entry may be tightened to pay for them. What may not move is the
# height of the section, which is what holds the page.
BLOCK_SECTIONS = ("PROJECTS",)


def _check_lengths(original: str, tailored: str) -> list[str]:
    """Nothing may take more printed lines than it does now.

    Outside `BLOCK_SECTIONS` that is read per bullet: an EXPERIENCE bullet
    that grew a line pushes everything under it down. Inside PROJECTS it is
    read over the whole section, so the model may spend a line from one
    entry on another and still land on the same page.

    Growing raises, because the page compile is minutes later and this is
    string work. Coming back shorter is a warning: the page still fits, but
    a hole in the layout is worth seeing on the review page.
    """
    width = layout.chars_per_line()
    warnings, grew = [], []
    for index, before, after, section in _paired_bodies(original, tailored):
        if section in BLOCK_SECTIONS:
            continue
        was, _, _ = layout.budget(before, width)
        now, cap, used = layout.budget(after, width)
        if now > was:
            grew.append(f"bullet {index}: {was} lines -> {now} "
                        f"({used} characters, {was * width + layout.SLACK} fit)")
        elif now < was:
            warnings.append(f"bullet {index} lost a line ({was} -> {now})")

    for name in BLOCK_SECTIONS:
        was, now = _section_lines(original, name, width), _section_lines(tailored, name, width)
        if was is None or now is None:
            continue
        if now > was:
            # With the arithmetic, not without it: this used to say only "cut
            # the other entries to pay for the one you grew", the one
            # rejection in the set that handed the model no number, and a
            # PROJECTS overrun then cost an attempt as often as not.
            grew.append(f"{name} as a whole: {was} lines -> {now}. {_block_overrun(original, tailored, name, width)}")
        elif now < was:
            warnings.append(f"{name} lost {was - now} line(s) ({was} -> {now})")

    more, softer = _check_single_items(original, tailored, width)
    grew += more
    warnings += softer
    if grew:
        raise TailorError("these grew past their line budget: " + "; ".join(grew))
    return warnings


def _block_chars(tex: str, name: str, width: int) -> tuple[int, int]:
    """(printed lines, visible characters) of one block section."""
    bodies = [body for body, section in _bullets_with_sections(tex) if section == name]
    lines = sum(layout.line_count(body, width) for body in bodies)
    return lines, sum(len(layout.visible(body)) for body in bodies)


def _block_overrun(original: str, tailored: str, name: str, width: int) -> str:
    """What to cut from a block section, in characters, per entry."""
    was, _ = _block_chars(original, name, width)
    _, used = _block_chars(tailored, name, width)
    fits = was * width + layout.SLACK
    bodies = [body for body, section in _bullets_with_sections(tailored) if section == name]
    entries = "; ".join(f"entry {i} {len(layout.visible(body))}"
                        for i, body in enumerate(bodies, 1))
    return (f"The section holds {fits} characters across its {was} lines and you "
            f"returned {used} ({entries}). Cut {max(1, used - fits)} characters from "
            "the entries, whichever of them the posting needs least, and keep the "
            "one you grew. Do not drop an entry.")


def _section_lines(tex: str, name: str, width: int) -> Optional[int]:
    """Printed lines every bullet of one section takes, together."""
    bodies = [body for body, section in _bullets_with_sections(tex) if section == name]
    if not bodies:
        return None
    return sum(layout.line_count(body, width) for body in bodies)


def _bullets_with_sections(tex: str) -> list[tuple[str, str]]:
    """Every bullet body with the section heading it sits under."""
    out = []
    for name, body in split_sections(tex).items():
        for item in bullets(body):
            out.append((item, name))
    return out


SKILL_LINE_RE = structure.SKILL_LINE


def _summary(tex: str) -> str:
    return structure.summary(tex)


def _skill_lines(tex: str) -> list[str]:
    """One entry per line of the skills section (`tailor/structure.py`)."""
    return structure.skill_lines(tex)


def single_items(tex: str) -> list[tuple[str, str]]:
    """The named parts measured one by one outside the bullets: the summary
    and each skills line. Same names the checker uses, so a rejection and
    the budget it broke read as the same thing."""
    items = [("the summary", _summary(tex))]
    items += [(f"skills line {i}", line) for i, line in enumerate(_skill_lines(tex), 1)]
    return items


def _check_single_items(original: str, tailored: str, width: int) -> tuple[list[str], list[str]]:
    """The summary and each skills line hold their printed lines too. They
    are not bullets, so they are matched by position: neither may be
    reordered, added or dropped."""
    grew, warnings = [], []
    items = [("the summary", _summary(original), _summary(tailored))]
    before, after = _skill_lines(original), _skill_lines(tailored)
    if len(before) == len(after):
        items += [(f"skills line {i}", b, a) for i, (b, a) in enumerate(zip(before, after), 1)]
    else:
        grew.append(f"the skills lines changed count ({len(before)} -> {len(after)})")
    for name, b, a in items:
        if not b or not a:
            continue
        was, _, _ = layout.budget(b, width)
        now, _, used = layout.budget(a, width)
        if now > was:
            grew.append(f"{name}: {was} lines -> {now} ({used} characters, "
                        f"{was * width + layout.SLACK} fit)")
        elif now < was:
            warnings.append(f"{name} lost a line ({was} -> {now})")
    return grew, warnings


def _paired_bodies(original: str, tailored: str) -> list[tuple[int, str, str, str]]:
    """Master bullet body next to the tailored one it became, with the
    section it sits under. Matched by similarity rather than position,
    because the rules allow reordering and swapping entries."""
    before_pairs = _bullets_with_sections(original)
    after_pairs = _bullets_with_sections(tailored)
    before = [body for body, _ in before_pairs]
    after = [body for body, _ in after_pairs]
    if len(before) != len(after):
        raise TailorError(
            f"bullet count changed ({len(before)} -> {len(after)}); "
            "the layout is tuned for exactly this many"
        )
    scores = sorted(
        ((difflib.SequenceMatcher(None, _normalise(a), _normalise(b)).ratio(), i, j)
         for i, a in enumerate(before) for j, b in enumerate(after)),
        reverse=True,
    )
    taken_before: set[int] = set()
    taken_after: set[int] = set()
    pairs: list[tuple[int, str, str, str]] = []
    for _, i, j in scores:
        if i in taken_before or j in taken_after:
            continue
        taken_before.add(i)
        taken_after.add(j)
        pairs.append((i + 1, before[i], after[j], before_pairs[i][1]))
        if len(pairs) == len(before):
            break
    return sorted(pairs)


def overrun_report(original: str, tailored: str) -> str:
    """Name the bullets that overflowed, worst first, in lines and in the
    characters that would fit.

    A generic "it is too long" leaves the model guessing at which of eleven
    bullets to cut, and it usually guesses wrong. Handing it the arithmetic
    turns the retry into an edit rather than another attempt.
    """
    width = layout.chars_per_line()
    try:
        pairs = _paired_bodies(original, tailored)
    except TailorError:
        return "The bullet count changed, which is itself the problem."

    grew = []
    for index, before, after, section in pairs:
        if section in BLOCK_SECTIONS:
            continue
        was, _, _ = layout.budget(before, width)
        now, _, used = layout.budget(after, width)
        if now > was:
            grew.append((index, was, now, used, was * width + layout.SLACK))

    # Block sections were skipped here, so a second page caused by PROJECTS
    # got "no bullet grew, look elsewhere" - the one overflow the report could
    # not name. It is the arithmetic of the pool that names it.
    blocks = []
    for name in BLOCK_SECTIONS:
        was, now = _section_lines(original, name, width), _section_lines(tailored, name, width)
        if was is not None and now is not None and now > was:
            blocks.append(f"- {name}: {was} printed lines before, {now} now. "
                          + _block_overrun(original, tailored, name, width))

    if not grew and not blocks:
        return (
            "No bullet grew past its line budget, so the overflow is elsewhere: "
            "check the summary and the skills lines, which must also keep the "
            "number of printed lines they have."
        )
    if not grew:
        return "This section runs onto an extra line:\n" + "\n".join(blocks)

    grew.sort(key=lambda row: row[3] - row[4], reverse=True)
    lines = "\n".join(
        f"- bullet {index}: {was} lines before, {now} now; {used} characters "
        f"where {fits} fit. Cut {used - fits}."
        for index, was, now, used, fits in grew[:6]
    )
    report = f"These bullets run onto an extra line:\n{lines}"
    return report + ("\n" + "\n".join(blocks) if blocks else "")


# ------------------------------------------------------- what was invented --

# A number on a resume is a claim the reader can check, and until now nothing
# checked it: links and counts were verified, figures never were. Both premium
# models wrote some that are true of nothing on file ("100+ jobs in a week",
# falsification gaps that no story mentions). Every figure in the tailored
# resume has to come from the master resume or from a story.
# Not a digit inside a name: "k6", "p95" and "S3" are terms, checked as
# terms; a figure is a number that stands on its own.
NUMBER = re.compile(r"(?<![A-Za-z0-9.])\d[\d,]*(?:\.\d+)?")
# A term worth checking: a word carrying a capital letter or a digit, which is
# how a stack, a product, a metric and a proper noun read. Ordinary prose words
# are the model's to choose; "Kubernetes" is not.
TERM = re.compile(r"[A-Za-z][A-Za-z0-9+#.-]*")
# Both sides are read with this: the master writes "10K/hour", and a piece of
# a token must never look new on its own.
WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9+#.-]*")


def _visible_text(tex: str) -> str:
    """What a reader sees in the parts the model may edit: every bullet, the
    summary and the skills lines, macros stripped."""
    parts = [layout.visible(body) for body in bullets(tex)]
    parts += [layout.visible(body) for _, body in single_items(tex) if body]
    # One fragment per line: a bullet's first word starts a sentence, and the
    # term scan has to be able to see that.
    return "\n".join(parts)


def _numbers(text: str) -> set[str]:
    return {match.group(0).replace(",", "").rstrip(".") for match in NUMBER.finditer(text)}


# The first word of a sentence is capitalised because it is first, not
# because it names anything. "Served 50K requests" would otherwise read as
# a product called Served.
# A colon does not start a sentence: a story's "Stack: Memcached" would
# lose the very word it is there to license.
SENTENCE_START = re.compile(r"(?:^|(?<=[.!?])\s+|(?<=\n)\s*)[A-Za-z][A-Za-z0-9+#.]*")


def _stem(word: str) -> str:
    """Enough of a stem that a rewrite is not mistaken for an invention:
    "fine-tuning" is the master's "fine-tuned", and a bullet rewritten in
    another tense says nothing new."""
    word = word.lower()
    for suffix in ("ing", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


def _named(text: str) -> dict[str, str]:
    """Stem -> the word as it was written. A name is a word carrying a
    capital or a digit; a word capitalised only because it opens a sentence
    is not one."""
    found: dict[str, str] = {}
    # The same token pattern as the known side, so "50K+" is one word on both
    # and a piece of it never looks new on its own.
    for match in WORD.finditer(SENTENCE_START.sub(" ", text)):
        word = match.group(0).strip(".-")
        if len(word) < 2 or not any(c.isalpha() for c in word):
            continue
        if not (any(c.isupper() for c in word) or any(c.isdigit() for c in word)):
            continue
        for part in word.split("-"):
            if len(part) > 1:
                found.setdefault(_stem(part), part)
    return found


def _terms(text: str) -> set[str]:
    """The stems of every name in `text`."""
    return set(_named(text))


def _words(text: str) -> set[str]:
    """The stem of every word in `text`, names or not."""
    return {_stem(part) for match in WORD.finditer(text)
            for part in match.group(0).strip(".-").split("-") if len(part) > 1}


def _is_known(stem: str, known: set[str]) -> bool:
    """Is this stem already on file? Read loosely on purpose: "Dec" is the
    master's December and "Tracker" is its GitTrack. What the check is for is
    a name that is on file nowhere at all, in any form.
    """
    if stem in known:
        return True
    return len(stem) >= 3 and any(stem in word for word in known)


def _check_invented(original: str, tailored: str, profile: str = "", posting: str = "") -> None:
    """No figure and no named thing that is not already on file.

    Two sources, deliberately different. A **figure** may come only from the
    master resume or a story: a number is a claim about what the candidate
    did, and a posting's own numbers say nothing about them. A **term** may
    also come from the posting, because naming the stack the way the posting
    names it is the point of tailoring; what it may not be is from nowhere.
    """
    from_model = _visible_text(tailored)
    # Everything the master says, not only the parts the model may edit: the
    # education line holds "MS", and a word the master writes in lower case
    # ("fine-tuned") licenses the tailored resume's "Fine-Tuning".
    on_file = layout.visible(original) + "\n" + (profile or "")

    invented = sorted(_numbers(from_model) - _numbers(on_file))
    if invented:
        raise TailorError(
            "tailored resume states figures that are on neither the master resume "
            f"nor a story: {', '.join(invented)}. Use the master's own numbers, in "
            "the master's own spelling"
        )

    # Known is every word on file, not only the ones that read as names: the
    # master's own prose is as good a source for a term as its skills line.
    known = _words(on_file) | _words(posting or "")
    strange = sorted(word for stem, word in _named(from_model).items()
                     if not _is_known(stem, known))
    if strange:
        raise TailorError(
            "tailored resume names things that are on neither the master resume, "
            f"a story, nor the posting: {', '.join(strange)}"
        )


# ---------------------------------------------------------- how much moved --

# How many of the EXPERIENCE bullets a run is expected to rewrite. The
# tailoring prompt asks for every mapped bullet; prose alone is not enough for
# a cheap model, which is the whole finding of the six-model comparison
# (2026-09-24: Opus rewrote 5 of 6 bullets, GLM 1 of 6, on the same prompt).
# Every budget that is enforced and named gets obeyed; this makes the volume
# one of them. A fraction, not a count, because a shorter resume has fewer
# bullets to spend.
MIN_REWRITE = float(os.environ.get("AUTOPILOT_MIN_REWRITE", "0.5"))


def rewritten(original: str, tailored: str) -> tuple[list[int], list[int]]:
    """(master indices rewritten, master indices left alone), EXPERIENCE only.

    Matched by similarity like every other bullet comparison, so reordering
    is not mistaken for a rewrite.
    """
    changed, kept = [], []
    for index, before, after, section in _paired_bodies(original, tailored):
        if section != "EXPERIENCE":
            continue
        (changed if _normalise(before) != _normalise(after) else kept).append(index)
    return changed, kept


def under_tailored(original: str, tailored: str) -> Optional[str]:
    """The run that barely touched the resume, named. None when it is fine.

    Never fatal: it is raised as a rejection while attempts are left and
    carried as a warning on the last one. A resume that could honestly not be
    improved is a worse outcome as a failed job than as a thin one.
    """
    try:
        changed, kept = rewritten(original, tailored)
    except TailorError:
        return None
    total = len(changed) + len(kept)
    if not total:
        return None
    floor = math.ceil(total * MIN_REWRITE)
    if len(changed) >= floor:
        return None
    return (f"only {len(changed)} of {total} EXPERIENCE bullets were rewritten; "
            f"this run is expected to rewrite at least {floor}. Untouched: "
            + ", ".join(f"bullet {i}" for i in kept))


# ------------------------------------------------ the swap that never was --

# A story the resume does not carry reaches the prompt marked as one that may
# take a PROJECTS entry's place. Half of the runs that had such a story used
# none of them (18 of 36 jobs, 2026-09-25), and roughly a third of those kept
# an entry the posting asked nothing about. The volume floor fixed the same
# thing for EXPERIENCE bullets, so this is the same shape: named, counted, and
# rejected while attempts remain.
SWAP_TERMS = int(os.environ.get("AUTOPILOT_SWAP_TERMS", "2"))
CANDIDATE_HEAD = re.compile(r"^### (\S+) \(not on the resume", re.M)


def swap_candidates(profile_text: str) -> list[tuple[str, str]]:
    """(slug, its story) for every story in the prompt that the resume does
    not already carry. The marker is written by `profile.stories`."""
    heads = list(CANDIDATE_HEAD.finditer(profile_text or ""))
    out = []
    for i, head in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(profile_text)
        out.append((head.group(1), profile_text[head.end():end]))
    return out


def unused_swap(tailored: str, profile_text: str, posting_text: str) -> Optional[str]:
    """The story that matched the posting and was left on the shelf, named.

    None when a candidate's link is on the tailored resume (a swap happened),
    when no candidate shares `SWAP_TERMS` stack terms with the posting, or when
    the ones that match have no `Link:` line - a story without a link may never
    be swapped in, so demanding one would be a rejection nothing can satisfy.
    """
    from .profile import stack_overlap

    links = set(HREF.findall(tailored))
    matched = []
    for slug, text in swap_candidates(profile_text):
        link = LINK_LINE.search(text)
        if not link:
            continue
        if link.group(1) in links:
            return None
        terms = stack_overlap(text, posting_text or "")
        if len(terms) >= SWAP_TERMS:
            matched.append((slug, sorted(terms)))
    if not matched:
        return None
    named = "; ".join(f"{slug} ({', '.join(terms)})" for slug, terms in matched[:3])
    return ("no PROJECTS entry was swapped, though the posting names the stack of a "
            f"story that is not on the resume yet: {named}. Swap the weakest entry "
            "for the one the posting asks for, one out and one in, or say in the "
            "rationale why every entry you kept beats it")


HREF = re.compile(r"\\href\{([^}]*)\}")
LINK_LINE = re.compile(r"^Link:\s*(\S+)\s*$", re.M)


def allowed_links(original: str, profile: str) -> set[str]:
    """Every URL the tailored resume may hyperlink: the master's own, and
    the Link line of each story in the profile context. A project swapped
    in from a story carries its link; any other new URL is invented."""
    return set(HREF.findall(original)) | set(LINK_LINE.findall(profile or ""))


def _check_links(original: str, tailored: str, profile: str) -> None:
    allowed = allowed_links(original, profile)
    strange = [url for url in HREF.findall(tailored) if url not in allowed]
    if strange:
        raise TailorError("tailored resume links to a URL that is neither on the master "
                          f"resume nor a story's Link line: {', '.join(sorted(set(strange)))}")


def _validate(original: str, tailored: str, profile: str = "", posting: str = "") -> list[str]:
    """Structural checks, cheapest first. Returns non-fatal warnings."""
    if not tailored.strip():
        raise TailorError("model returned an empty resume")
    if tailored.count("{") != tailored.count("}"):
        raise TailorError("tailored resume has unbalanced braces")
    for command in (r"\documentclass", r"\begin{document}", r"\end{document}"):
        if command in original and command not in tailored:
            raise TailorError(f"tailored resume dropped {command}")
    _check_frozen_sections(original, tailored)
    _check_links(original, tailored, profile)
    # Lengths first: that is the check that protects the page, and a bullet
    # that overflowed is a more specific thing to be told than a figure in it.
    warnings = _check_lengths(original, tailored)
    _check_invented(original, tailored, profile, posting)
    return warnings


def load_base_resume(path: Path = BASE_RESUME) -> str:
    if not path.exists():
        raise TailorError(
            f"no master resume at {path}. Export your resume .tex and place it there."
        )
    return path.read_text()


def load_profile(path: Optional[Path] = None, stories: Optional[list[str]] = None) -> str:
    """profile.md, with applicant facts and the picked stories when the switch is on."""
    return profile_module.context(profile_module.load_profile(path or PROFILE), slugs=stories)


def _build_user_message(posting: Posting, resume: str, profile: str) -> str:
    width = layout.chars_per_line()
    # Every bullet is numbered over the whole document, because that is how a
    # rejection names one; only the editable ones are listed, since a budget
    # for a frozen section is a budget the model may not spend.
    # Numbered the way a rejection numbers them - over `_bullets_with_sections`,
    # which counts the `\item` entries of EDUCATION and the skills block too.
    # The list used to be built over `bullets()` instead, which sees only
    # `\resumeItem`, so "bullet 1" in the budget was "bullet 3" in the
    # rejection that enforced it. Only the editable ones are listed: a budget
    # for a frozen section is a budget the model may not spend, and the summary
    # and the skills lines have their own rows below.
    rows = []
    for i, (body, section) in enumerate(_bullets_with_sections(resume), 1):
        if section not in EDITABLE_SECTIONS or section in BLOCK_SECTIONS:
            continue
        if section in ("SUMMARY", "TECHNICAL SKILLS"):
            continue
        rows.append((f"bullet {i}", *layout.room(body, width)))
    # The summary and each skills line are held to their printed lines the
    # same way (`_check_single_items`), and used not to be listed here: a
    # DoorDash run spent all four attempts on a summary it had never been
    # given a size for, and an earlier one on skills line 7. A budget the
    # checker enforces has to be a budget the model was told.
    rows += [(name, *layout.room(body, width))
             for name, body in single_items(resume) if body]
    budget = "\n".join(
        f"- {name}: {lines} printed line{'s' if lines != 1 else ''}, "
        f"{used} characters now, {fits - used} spare before it takes another line"
        for name, lines, fits, used in rows
    )

    sections = [f"## Job posting\n\n{posting.to_markdown()}"]
    if profile:
        sections.append(
            "## Candidate profile (background not necessarily on the resume)\n\n" + profile
        )
    sections.append(
        "## The line budget — what keeps this resume on one page\n\n"
        f"One printed line of this resume holds about {width} characters. Each "
        "EXPERIENCE bullet must print on the same number of lines as it does now. "
        "The word count is yours to change: write to the budget, not around it.\n\n"
        "The last line of an item is usually only part full, and the spare below "
        "is what is left on it, counted down rather than up: spend it and the item "
        "still prints on the same number of lines. The numbers are this resume's "
        "own bullet numbering, which is how a rejection will name one; a bullet "
        "missing from the list is in a frozen section or measured below as the "
        "summary or a skills line."
        + block_budgets(resume, width)
        + f"\n\n{budget}"
    )
    sections.append(layout_note(resume))
    sections.append(f"## Master resume LaTeX\n\n```tex\n{resume}\n```")
    return "\n\n".join(sections)


def block_budgets(resume: str, width: Optional[int] = None) -> str:
    """The pooled budget of each block section, per entry and in total.

    A block section is measured over all of its entries, and until now the
    prompt said so in lines only while still listing each entry its own
    character cap - a per-entry rule to read and a pooled rule to be judged
    by. A longer entry swapped in then looked illegal on the numbers the model
    had, which is one reason a second project was never swapped. So the pool
    is spelled out: what each entry uses, what the section holds, what is
    spare across it.
    """
    width = width or layout.chars_per_line()
    out = []
    for name in BLOCK_SECTIONS:
        bodies = [body for body, section in _bullets_with_sections(resume)
                  if section == name]
        if not bodies:
            continue
        lines = sum(layout.line_count(body, width) for body in bodies)
        used = sum(len(layout.visible(body)) for body in bodies)
        fits = max(used, lines * width)
        entries = "; ".join(
            f"entry {i} {len(layout.visible(body))} characters"
            for i, body in enumerate(bodies, 1)
        )
        out.append(
            f"\n\n{name} is measured as one block, not entry by entry: its "
            f"{len(bodies)} entries take {lines} printed lines together today and "
            f"must take exactly that many when you return them. Together they hold "
            f"{fits} characters and use {used} ({entries}), so there are "
            f"{fits - used} spare to move between them. An entry may grow as far as "
            f"the others are cut to pay for it: that is how a project worth more "
            f"space than the one it replaces gets it."
        )
    return "".join(out)


def layout_note(resume: str) -> str:
    """The rules name four sections; this resume may call them anything.
    Said per resume, so "EXPERIENCE" in the rules is the heading the person
    wrote, and the design they chose is left exactly as it is."""
    found = structure.titles(resume)
    roles = [f"- {role}: the section headed \"{found[role]}\"" if role in found
             else f"- {role}: this resume has none; leave it that way"
             for role in EDITABLE_SECTIONS]
    frozen = [f'"{title}"' for key, title in found.items() if key not in EDITABLE_SECTIONS]
    bullet = (r"`\resumeItem{...}`" if structure.uses_resume_item(resume)
              else r"`\item` lines")
    return ("## This resume's layout\n\n"
            "The resume is in the person's own LaTeX design. Keep every macro, "
            "environment, spacing command and heading exactly as it is; change only "
            "the words inside the sections below.\n\n"
            + "\n".join(roles)
            + (f"\n\nFrozen, word for word: {', '.join(frozen)}." if frozen else "")
            + f"\n\nBullets in this resume are {bullet}; a bullet stays one of those.")


def tailor(
    posting: Posting,
    resume_tex: Optional[str] = None,
    page_check: Optional[Callable[[str], Optional[int]]] = None,
    target_pages: int = 1,
    max_attempts: int = 4,
    extra_instruction: str = "",
    profile: Optional[str] = None,
    model: Optional[str] = None,
) -> TailorResult:
    """Tailor the resume, retrying while it does not fit on `target_pages`.

    `page_check` compiles a candidate and returns its page count, or None when
    the page count could not be determined. Passing None skips the loop, which
    is what the unit tests do; the pipeline always passes a real compiler.
    """
    original = resume_tex if resume_tex is not None else load_base_resume()
    if profile is None:
        profile = load_profile()
    # A job captured with "Use Opus" carries its own model; everything else
    # runs on the cheap default.
    model = model or llm.tailor_model()

    message = _build_user_message(posting, original, profile)
    if extra_instruction.strip():
        # The reviewer's words carry more weight than the model's own earlier
        # judgement, but not more than the rules: they are appended to the
        # request, never to the system prompt.
        message += (
            "\n\n## The reviewer asked for a change\n\n"
            f"{extra_instruction.strip()}\n\n"
            "Apply this while still obeying every rule above."
        )
    system = system_prompt()
    feedback = ""

    last_error: Optional[str] = None
    rejected: list[dict] = []
    preamble_notes: list[str] = []
    for attempt in range(1, max_attempts + 1):
        reply = llm.complete(system, message + feedback, model=model)

        verdict = _extract_block(reply, ("verdict",)) or ""
        if verdict.strip().upper().startswith("MISMATCH"):
            raise Mismatch(verdict.strip())

        tailored = _extract_block(reply, ("tex", "latex"))
        if tailored is not None:
            tailored, restored = restore_preamble(original, tailored)
            if restored:
                # Worth seeing on the review page: it means the model is
                # spending attention above \\begin{document}, where there is
                # nothing to win.
                spliced = "the preamble was restored from the master"
                if spliced not in preamble_notes:
                    preamble_notes.append(spliced)
        if tailored is None:
            last_error = "model reply contained no ```tex block"
            rejected.append({"attempt": attempt, "reason": last_error, "tex": reply})
            feedback = f"\n\n## Your previous attempt failed\n\n{last_error}. Try again."
            continue

        # A model squeezed by the length rules will sometimes return the file
        # untouched, which is a non-answer rather than a tailoring pass.
        if _normalise(tailored) == _normalise(original):
            last_error = "returned the resume unchanged"
            feedback = (
                "\n\n## Your previous attempt was rejected\n\n"
                "You returned the resume unchanged. Tailoring it is the task. "
                "Find the places where the posting names something the candidate "
                "has actually done and uses a different word for it, and adopt the "
                "posting's word. Reorder the projects and the skills lines so the "
                "most relevant come first. Keep every length the same by trading: "
                "if a phrase gets longer, shorten another in the same bullet.\n\n"
                "If after genuinely looking there is nothing truthful to change, "
                "say so in the rationale and return the file unchanged again."
            )
            if attempt < max_attempts:
                rejected.append({"attempt": attempt, "reason": last_error, "tex": tailored})
                continue

        try:
            warnings = _validate(original, tailored, profile, posting.text)
        except TailorError as exc:
            last_error = str(exc)
            rejected.append({"attempt": attempt, "reason": last_error, "tex": tailored})
            feedback = (
                f"\n\n## Your previous attempt was rejected\n\n{last_error}\n\n"
                "Return the whole file again with that fixed. Keep every other edit "
                "you made: the tailoring is wanted, only the named problem is not. "
                "Reverting to the master resume is a worse answer than the attempt "
                "you just sent."
            )
            continue

        # How much moved. Rejected while there are attempts left, carried as a
        # warning on the last one: a thin resume is worth seeing on the review
        # page, a failed job is not.
        thin = under_tailored(original, tailored)
        if thin:
            if attempt < max_attempts:
                last_error = thin
                rejected.append({"attempt": attempt, "reason": thin, "tex": tailored})
                feedback = (
                    f"\n\n## Your previous attempt was rejected\n\n{thin}\n\n"
                    "Rewrite those bullets too. Each one keeps its printed line "
                    "count and every fact it states, and takes the X-Y-Z frame: "
                    "past-tense verb, the result before the method, a measure or "
                    "an honest scope, and the stack named the way the posting "
                    "names it. Keep the edits you have already made."
                )
                continue
            warnings = warnings + [thin]

        # The same shape as the volume floor: a rejection while there are
        # attempts left, a warning on the last one, because a resume that kept
        # its projects is worth shipping and a failed job is not.
        missed = unused_swap(tailored, profile, posting.text)
        if missed:
            if attempt < max_attempts:
                last_error = missed
                rejected.append({"attempt": attempt, "reason": missed, "tex": tailored})
                feedback = (
                    f"\n\n## Your previous attempt was rejected\n\n{missed}.\n\n"
                    "One entry out, one in, the section back to the same printed "
                    "lines, the new entry's Link line as its only URL. Keep every "
                    "other edit you have made."
                )
                continue
            warnings = warnings + [missed]

        if page_check is not None:
            pages = page_check(tailored)
            if pages is not None and pages != target_pages:
                last_error = f"compiled to {pages} pages, must be {target_pages}"
                rejected.append({"attempt": attempt, "reason": last_error, "tex": tailored})
                feedback = (
                    f"\n\n## Your previous attempt was rejected\n\n"
                    f"It {last_error}.\n\n{overrun_report(original, tailored)}\n\n"
                    "Return the whole file again with those bullets cut back inside "
                    "their line budgets. Keep every other edit you made; do not revert "
                    "to the master resume. Do not delete a bullet, do not remove a "
                    "section, and do not touch the spacing knobs in the preamble."
                )
                continue

        suggestions = _extract_block(reply, ("markdown", "md")) or "_No rationale returned._"
        return TailorResult(
            tex=tailored,
            suggestions=suggestions,
            diff=make_diff(original, tailored),
            model=model,
            verdict=verdict.strip(),
            attempts=attempt,
            warnings=warnings + preamble_notes,
            rejections=[f"attempt {r['attempt']}: {r['reason']}" for r in rejected],
        )

    raise TailorError(
        f"gave up after {max_attempts} attempts; last problem: {last_error}", rejected
    )
