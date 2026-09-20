"""Which applicant-tracking system a form belongs to, and what to tell the
browser model about it.

The notes live in `browser/ats_rules.md`, one `## name` section per system,
and go into the task verbatim. Detection is by URL only: the systems each
have a host, or a query marker in the case of Greenhouse embedded on a
careers page. An unknown system gets no notes and the task as it was.
"""
from __future__ import annotations

import re
from pathlib import Path

RULES = Path(__file__).resolve().parent / "ats_rules.md"

# Order matters only where markers overlap; each pattern is tried on the
# whole URL, lowercased.
SYSTEMS = [
    ("oracle", re.compile(r"oraclecloud\.com/.*hcmui/candidateexperience")),
    ("greenhouse", re.compile(r"greenhouse\.io|[?&]gh_jid=")),
    ("ashby", re.compile(r"ashbyhq\.com")),
    ("workday", re.compile(r"myworkdayjobs\.com|myworkdaysite\.com")),
    ("lever", re.compile(r"jobs\.lever\.co")),
]


def detect(url: str) -> str:
    lowered = (url or "").lower()
    for name, pattern in SYSTEMS:
        if pattern.search(lowered):
            return name
    return ""


def sections(path: Path | None = None) -> dict[str, str]:
    text = (path or RULES).read_text()
    out: dict[str, str] = {}
    for match in re.finditer(r"^## (\w+)\s*\n(.*?)(?=^## |\Z)", text, re.S | re.M):
        out[match.group(1).lower()] = match.group(2).strip()
    return out


def notes(name: str, path: Path | None = None) -> str:
    """The section for `name`, or empty."""
    if not name:
        return ""
    return sections(path).get(name, "")


def task_block(url: str) -> str:
    """What goes into the task: a heading and the notes, or nothing."""
    name = detect(url)
    body = notes(name)
    if not body:
        return ""
    return f"\nNotes for this application system ({name}):\n{body}\n"
