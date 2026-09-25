"""What a resume's LaTeX is made of, whatever its design.

Every person keeps their own LaTeX resume: their template, their macros,
their look. The tailor adapts to it rather than the other way round. This
module finds, in any of them:

- the **section headings**, and which of them play the four roles the
  tailoring rules name (SUMMARY, EXPERIENCE, PROJECTS, TECHNICAL SKILLS);
  every other section is frozen;
- the **bullets**: `\\resumeItem{...}` when the resume uses it (Jake's
  template and its many descendants), otherwise every `\\item`;
- the **skills lines** and the **summary**, which are measured one by one.

Headings are found as a command with "section" in its name (`\\section`,
`\\section*`, `\\cvsection`, `\\resumeSection`...), its first argument read
as text (`\\texorpdfstring{A}{B}` is A, `\\color{x}` is dropped). A resume
with fewer than two of those is read by its words instead: any command
whose argument is a known heading ("Experience", "Skills"...), which covers
`\\textbf{EXPERIENCE}` and the like. Nothing here writes LaTeX; it only
reads, so a layout it misreads is a rejected tailoring attempt, never a
damaged resume.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

# The four roles the rules name, and the headings people give them.
ROLES: dict[str, tuple[str, ...]] = {
    "SUMMARY": ("summary", "professional summary", "profile", "professional profile", "objective",
                "career objective", "about", "about me", "overview", "summary of qualifications"),
    "EXPERIENCE": ("experience", "work experience", "professional experience", "employment",
                   "employment history", "work history", "relevant experience",
                   "industry experience", "internships", "internship experience",
                   "research experience", "experience and internships"),
    "PROJECTS": ("projects", "personal projects", "selected projects", "academic projects",
                 "technical projects", "side projects", "project experience", "key projects",
                 "projects and research"),
    "TECHNICAL SKILLS": ("skills", "technical skills", "technologies", "technical expertise",
                         "core skills", "skills and technologies", "skills & technologies",
                         "tools and technologies", "programming skills", "skills and tools",
                         "technical proficiencies", "core competencies"),
}
ROLE_OF = {alias: role for role, aliases in ROLES.items() for alias in aliases}

# Headings that are not one of the roles, for the fallback by words.
OTHER_HEADINGS = ("education", "achievements", "awards", "honors", "honors and awards",
                  "publications", "certifications", "leadership", "activities",
                  "extracurricular activities", "volunteer", "volunteering", "interests",
                  "languages", "coursework", "relevant coursework", "research", "teaching")

COMMAND = re.compile(r"\\([A-Za-z]+\*?)\s*\{")
RESUME_ITEM = re.compile(r"\\resumeItem\s*\{")
ITEM = re.compile(r"\\item\b\s*(?:\[[^\]]*\])?")
LIST_END = re.compile(r"\\end\{[A-Za-z*]+\}|\\resume[A-Za-z]*ListEnd\b")
SKILL_LINE = re.compile(r"\\textbf\s*\{[^\n]*?\}[^\n]*", re.S)


@dataclass(frozen=True)
class Heading:
    key: str        # the role, or the heading's own words in capitals
    title: str      # the heading as it reads on the page
    start: int      # where the heading command starts
    end: int        # just after its argument


def body_start(tex: str) -> int:
    index = tex.find(r"\begin{document}")
    return 0 if index == -1 else index + len(r"\begin{document}")


def commented(tex: str, index: int) -> bool:
    """Whether `index` sits after an unescaped % on its line."""
    line = tex[tex.rfind("\n", 0, index) + 1:index]
    return re.search(r"(?<!\\)%", line) is not None


def balanced(tex: str, open_index: int) -> Optional[str]:
    """Text between the brace at `open_index` and its matching close."""
    depth = 0
    for index in range(open_index, len(tex)):
        char = tex[index]
        if char == "{" and (index == 0 or tex[index - 1] != "\\"):
            depth += 1
        elif char == "}" and tex[index - 1] != "\\":
            depth -= 1
            if depth == 0:
                return tex[open_index + 1:index]
    return None


def heading_text(arg: str) -> str:
    """A heading's argument as the words on the page."""
    text = re.sub(r"\\texorpdfstring\s*\{((?:[^{}]|\{[^{}]*\})*)\}\s*\{(?:[^{}]|\{[^{}]*\})*\}", r"\1", arg)
    text = re.sub(r"\\(?:color|textcolor|fontsize|hspace|vspace|rule)\s*(?:\[[^\]]*\])?\{[^}]*\}", "", text)
    text = re.sub(r"\\[A-Za-z]+\*?", " ", text)
    text = text.replace("\\&", "&")
    text = re.sub(r"[{}~]", " ", text)
    return " ".join(text.split())


def _normal(title: str) -> str:
    return re.sub(r"[^a-z& ]", "", title.lower()).strip()


def _key(title: str) -> str:
    return ROLE_OF.get(_normal(title), " ".join(title.upper().split()))


def headings(tex: str) -> list[Heading]:
    start = body_start(tex)
    found, by_words = [], []
    for match in COMMAND.finditer(tex, start):
        name = match.group(1)
        if commented(tex, match.start()):
            continue
        arg = balanced(tex, match.end() - 1)
        if arg is None:
            continue
        title = heading_text(arg)
        if not title:
            continue
        end = match.end() + len(arg) + 1
        heading = Heading(_key(title), title, match.start(), end)
        lowered = name.lower()
        if "section" in lowered and not lowered.startswith("sub") and name != "titleformat":
            found.append(heading)
        elif _normal(title) in ROLE_OF or _normal(title) in OTHER_HEADINGS:
            by_words.append(heading)
    if len(found) >= 2:
        return found
    # Headings by their words: `\textbf{EXPERIENCE}` and the like. A word
    # inside a bullet ("\textbf{Skills}: Python") is not a heading, so a
    # heading has to be the only thing on its line.
    alone = [h for h in by_words if _alone_on_line(tex, h)]
    return alone if len(alone) >= 2 else found


# What may sit beside a heading on its line: a group opening and a size
# command before it (`{\large \textbf{EXPERIENCE}}`), and after it closing
# braces, a line break with its spacing, a rule.
HEAD_BEFORE = re.compile(r"^(?:\s|\{|\\[A-Za-z]+\b)*$")
HEAD_AFTER = re.compile(r"^(?:\s|\}|\\\\(?:\[[^\]]*\])?|\\[vh]space\*?\{[^}]*\}"
                        r"|\\(?:hrule|hline|par|newline|medskip|smallskip|bigskip)\b|\\rule\{[^}]*\}\{[^}]*\})*$")


def _alone_on_line(tex: str, heading: Heading) -> bool:
    line_start = tex.rfind("\n", 0, heading.start) + 1
    line_end = tex.find("\n", heading.end)
    after = tex[heading.end:line_end if line_end != -1 else len(tex)]
    return bool(HEAD_BEFORE.match(tex[line_start:heading.start]) and HEAD_AFTER.match(after))


def sections(tex: str) -> dict[str, str]:
    """Heading key to its body; everything before the first heading is
    `PREAMBLE`. The layout's own words are the keys, except for the four
    roles, which take the rules' names."""
    marks = headings(tex)
    if not marks:
        return {"PREAMBLE": tex}
    out = {"PREAMBLE": tex[:marks[0].start]}
    for index, mark in enumerate(marks):
        end = marks[index + 1].start if index + 1 < len(marks) else len(tex)
        key = mark.key
        while key in out:           # two headings with the same words
            key += "'"
        out[key] = tex[mark.end:end]
    return out


def titles(tex: str) -> dict[str, str]:
    """Role (or key) to the heading as the resume words it."""
    return {mark.key: mark.title for mark in headings(tex)}


def first_heading(tex: str) -> Optional[int]:
    marks = headings(tex)
    return marks[0].start if marks else None


def uses_resume_item(tex: str) -> bool:
    start = body_start(tex)
    return any(not commented(tex, m.start()) for m in RESUME_ITEM.finditer(tex, start))


def bullets(tex: str) -> list[str]:
    """Every bullet body, in document order."""
    start = body_start(tex)
    found = []
    if uses_resume_item(tex):
        for match in RESUME_ITEM.finditer(tex, start):
            if commented(tex, match.start()):
                continue
            body = balanced(tex, match.end() - 1)
            if body is not None:
                found.append(body)
        return found
    items = [m for m in ITEM.finditer(tex, start) if not commented(tex, m.start())]
    stops = sorted([m.start() for m in items] + [m.start() for m in LIST_END.finditer(tex, start)]
                   + [h.start for h in headings(tex)] + [len(tex)])
    for match in items:
        stop = next(s for s in stops if s > match.start())
        body = tex[match.end():stop].strip()
        if body:
            found.append(body)
    return found


def skill_lines(tex: str) -> list[str]:
    """One entry per line of the skills section: `\\textbf{Category:}`
    lines when it has them, else its items, else its lines with a colon."""
    body = sections(tex).get("TECHNICAL SKILLS", "")
    if not body:
        return []
    lines = [line.strip() for line in SKILL_LINE.findall(body)]
    if lines:
        return lines
    items = bullets(body) if ITEM.search(body) else []
    if items:
        return items
    return [line.strip() for line in body.splitlines() if ":" in line and line.strip()]


def summary(tex: str) -> str:
    return sections(tex).get("SUMMARY", "")
