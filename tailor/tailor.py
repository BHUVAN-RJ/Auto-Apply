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
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import llm, profile as profile_module
from .fetch import Posting

ROOT = Path(__file__).resolve().parent.parent
BASE_RESUME = ROOT / "base" / "resume.tex"
PROFILE = ROOT / "base" / "profile.md"
RULES = Path(__file__).resolve().parent / "rules.md"

# Sections the model is allowed to rewrite. Anything else must come back
# byte-identical, and _check_frozen_sections verifies that.
EDITABLE_SECTIONS = ("SUMMARY", "EXPERIENCE", "PROJECTS", "TECHNICAL SKILLS")

# How far a bullet may drift in length before the layout is at risk. The
# prompt asks for +/-10; the check allows more slack so that a reasonable
# edit is not rejected over punctuation, while a rewrite still trips it.
LENGTH_TOLERANCE = 40

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
<one bullet per change, twelve words at most, `where: "old" -> "new" (why)`.
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
    return RULES.read_text() + "\n" + REPLY_FORMAT


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
    """Map section name to its body.

    The template writes headings as
    `\\section{\\texorpdfstring{\\color{airforceblue}NAME}{}}`, so the section
    name is the run of capitals before the closing braces. Everything before
    the first heading is returned under the key `PREAMBLE`.
    """
    pattern = re.compile(
        r"\\section\{\\texorpdfstring\{(?:\\color\{[^}]*\})?\s*([A-Z][A-Z &/]*[A-Z])\}\{\}\}"
    )
    matches = list(pattern.finditer(tex))
    if not matches:
        return {"PREAMBLE": tex}

    sections = {"PREAMBLE": tex[: matches[0].start()]}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(tex)
        sections[match.group(1).strip()] = tex[match.end(): end]
    return sections


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
    depth = 0
    for index in range(open_index, len(tex)):
        char = tex[index]
        if char == "{" and (index == 0 or tex[index - 1] != "\\"):
            depth += 1
        elif char == "}" and tex[index - 1] != "\\":
            depth -= 1
            if depth == 0:
                return tex[open_index + 1: index]
    return None


def bullets(tex: str) -> list[str]:
    """Body of every \\resumeItem, in document order.

    A regex cannot do this: bullet bodies contain nested braces from
    \\normalsize, \\href, and \\textbf, so the closing brace has to be found
    by counting depth.
    """
    # Scan the body only: the preamble contains \\newcommand{\\resumeItem}...,
    # whose definition would otherwise be counted as a bullet.
    start = tex.find(r"\begin{document}")
    if start == -1:
        start = 0
    found = []
    for match in re.finditer(r"\\resumeItem\s*\{", tex[start:]):
        body = _balanced_body(tex[start:], match.end() - 1)
        if body is not None:
            found.append(body)
    return found


def bullet_lengths(tex: str) -> list[int]:
    """Character length of every \\resumeItem body, in document order."""
    return [len(_normalise(body)) for body in bullets(tex)]


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


def _check_lengths(original: str, tailored: str) -> list[str]:
    """Warn on bullets that drifted far enough to threaten the page break.

    Returned as warnings rather than errors: the page-count check is the real
    gate, and this is the earlier, cheaper signal that something is off.
    """
    warnings = []
    for index, was, now in pair_bullets(original, tailored):
        if abs(now - was) > LENGTH_TOLERANCE:
            warnings.append(f"bullet {index} length {was} -> {now} chars")
    return warnings


def overrun_report(original: str, tailored: str) -> str:
    """Name the bullets that grew, longest overrun first.

    A generic "it is too long" leaves the model guessing at which of eleven
    bullets to cut, and it usually guesses wrong. Handing it the arithmetic
    turns the retry into an edit rather than another attempt.
    """
    try:
        pairs = pair_bullets(original, tailored)
    except TailorError:
        return "The bullet count changed, which is itself the problem."

    grew = [(index, was, now) for index, was, now in pairs if now > was]
    total = sum(now - was for _, was, now in grew)
    if not grew:
        return (
            "No bullet grew, so the overflow is elsewhere: check the summary and "
            "the skills lines, which must also keep their original lengths."
        )

    grew.sort(key=lambda row: row[2] - row[1], reverse=True)
    lines = "\n".join(
        f"- bullet {index}: was {was} characters, you returned {now} (+{now - was})"
        for index, was, now in grew[:6]
    )
    return (
        f"These bullets grew, {total} characters in total:\n{lines}\n"
        f"Cut at least {total} characters from them."
    )


def _validate(original: str, tailored: str) -> list[str]:
    """Structural checks, cheapest first. Returns non-fatal warnings."""
    if not tailored.strip():
        raise TailorError("model returned an empty resume")
    if tailored.count("{") != tailored.count("}"):
        raise TailorError("tailored resume has unbalanced braces")
    for command in (r"\documentclass", r"\begin{document}", r"\end{document}"):
        if command in original and command not in tailored:
            raise TailorError(f"tailored resume dropped {command}")
    _check_frozen_sections(original, tailored)
    return _check_lengths(original, tailored)


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
    lengths = bullet_lengths(resume)
    budget = "\n".join(f"- bullet {i}: {n} characters" for i, n in enumerate(lengths, 1))

    sections = [f"## Job posting\n\n{posting.to_markdown()}"]
    if profile:
        sections.append(
            "## Candidate profile (background not necessarily on the resume)\n\n" + profile
        )
    sections.append(
        "## Current bullet lengths — each tailored bullet must stay within "
        f"10 characters of these\n\n{budget}"
    )
    sections.append(f"## Master resume LaTeX\n\n```tex\n{resume}\n```")
    return "\n\n".join(sections)


def tailor(
    posting: Posting,
    resume_tex: Optional[str] = None,
    page_check: Optional[Callable[[str], Optional[int]]] = None,
    target_pages: int = 1,
    max_attempts: int = 4,
    extra_instruction: str = "",
    profile: Optional[str] = None,
) -> TailorResult:
    """Tailor the resume, retrying while it does not fit on `target_pages`.

    `page_check` compiles a candidate and returns its page count, or None when
    the page count could not be determined. Passing None skips the loop, which
    is what the unit tests do; the pipeline always passes a real compiler.
    """
    original = resume_tex if resume_tex is not None else load_base_resume()
    if profile is None:
        profile = load_profile()
    model = llm.tailor_model()

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
    for attempt in range(1, max_attempts + 1):
        reply = llm.complete(system, message + feedback, model=model)

        verdict = _extract_block(reply, ("verdict",)) or ""
        if verdict.strip().upper().startswith("MISMATCH"):
            raise Mismatch(verdict.strip())

        tailored = _extract_block(reply, ("tex", "latex"))
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
            warnings = _validate(original, tailored)
        except TailorError as exc:
            last_error = str(exc)
            rejected.append({"attempt": attempt, "reason": last_error, "tex": tailored})
            feedback = (
                f"\n\n## Your previous attempt was rejected\n\n{last_error}\n\n"
                "Return the whole file again, fixing only that."
            )
            continue

        if page_check is not None:
            pages = page_check(tailored)
            if pages is not None and pages != target_pages:
                last_error = f"compiled to {pages} pages, must be {target_pages}"
                rejected.append({"attempt": attempt, "reason": last_error, "tex": tailored})
                feedback = (
                    f"\n\n## Your previous attempt was rejected\n\n"
                    f"It {last_error}.\n\n{overrun_report(original, tailored)}\n\n"
                    "Return the whole file again with those bullets cut back to at "
                    "or below their original lengths. Do not delete a bullet, do not "
                    "remove a section, and do not touch the spacing knobs in the "
                    "preamble."
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
            warnings=warnings,
        )

    raise TailorError(
        f"gave up after {max_attempts} attempts; last problem: {last_error}", rejected
    )
