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

from . import layout, llm, profile as profile_module
from .fetch import Posting

ROOT = Path(__file__).resolve().parent.parent
BASE_RESUME = ROOT / "base" / "resume.tex"
PROFILE = ROOT / "base" / "profile.md"
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


SECTION_START = re.compile(r"\\section\{")


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
    here, there = SECTION_START.search(original), SECTION_START.search(tailored)
    if not here or not there:
        return tailored, False
    if _normalise(original[:here.start()]) == _normalise(tailored[:there.start()]):
        return tailored, False
    return original[:here.start()] + tailored[there.start():], True


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
            grew.append(f"{name} as a whole: {was} lines -> {now}; the section has to "
                        "come back the same height, so cut the other entries to pay "
                        "for the one you grew")
        elif now < was:
            warnings.append(f"{name} lost {was - now} line(s) ({was} -> {now})")

    more, softer = _check_single_items(original, tailored, width)
    grew += more
    warnings += softer
    if grew:
        raise TailorError("these grew past their line budget: " + "; ".join(grew))
    return warnings


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


SUMMARY_RE = re.compile(r"SUMMARY\}\{\}\}(.*?)(?=\\section|\Z)", re.S)
SKILL_LINE_RE = re.compile(r"\\textbf\s*\{[^\n]*?\}[^\n]*", re.S)


def _summary(tex: str) -> str:
    match = SUMMARY_RE.search(tex)
    return match.group(1) if match else ""


def _skill_lines(tex: str) -> list[str]:
    """One entry per `Category: items` line of TECHNICAL SKILLS."""
    start = tex.find("TECHNICAL SKILLS")
    if start == -1:
        return []
    end = tex.find(r"\section", start + 1)
    block = tex[start:end if end != -1 else len(tex)]
    return [line.strip() for line in SKILL_LINE_RE.findall(block)]


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
    if not grew:
        return (
            "No bullet grew past its line budget, so the overflow is elsewhere: "
            "check the summary and the skills lines, which must also keep the "
            "number of printed lines they have."
        )

    grew.sort(key=lambda row: row[3] - row[4], reverse=True)
    lines = "\n".join(
        f"- bullet {index}: {was} lines before, {now} now; {used} characters "
        f"where {fits} fit. Cut {used - fits}."
        for index, was, now, used, fits in grew[:6]
    )
    return f"These bullets run onto an extra line:\n{lines}"


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


def _validate(original: str, tailored: str, profile: str = "") -> list[str]:
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
    width = layout.chars_per_line()
    rows = [
        (f"bullet {i}", lines, cap, used)
        for i, (lines, cap, used) in enumerate(bullet_budgets(resume), 1)
    ]
    # The summary and each skills line are held to their printed lines the
    # same way (`_check_single_items`), and used not to be listed here: a
    # DoorDash run spent all four attempts on a summary it had never been
    # given a size for, and an earlier one on skills line 7. A budget the
    # checker enforces has to be a budget the model was told.
    rows += [(name, *layout.budget(body, width))
             for name, body in single_items(resume) if body]
    budget = "\n".join(
        f"- {name}: {lines} printed line{'s' if lines != 1 else ''}, "
        f"{used} characters now, up to {cap} and it still prints on {lines}"
        for name, lines, cap, used in rows
    )

    sections = [f"## Job posting\n\n{posting.to_markdown()}"]
    if profile:
        sections.append(
            "## Candidate profile (background not necessarily on the resume)\n\n" + profile
        )
    project_lines = _section_lines(resume, "PROJECTS", width)
    sections.append(
        "## The line budget — what keeps this resume on one page\n\n"
        f"One printed line of this resume holds about {width} characters. Each "
        "EXPERIENCE bullet must print on the same number of lines as it does now. "
        "The word count is yours to change: write to the budget, not around it."
        + (f"\n\nPROJECTS is measured as one block: its entries take {project_lines} "
           "printed lines together today and must take exactly that many when you "
           "return them. Move lines between the entries as the posting deserves, "
           "and write the shorter description wherever you can."
           if project_lines else "")
        + f"\n\n{budget}"
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
            warnings = _validate(original, tailored, profile)
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
