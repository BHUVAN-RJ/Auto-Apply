"""How many printed lines a bullet takes, and how many characters fit on one.

The length rule used to be "stay within N characters of what was there",
which made a real rewrite impossible: a bullet could only be re-punctuated.
What actually has to hold is the *page*, and what fills a page is lines, not
characters. A bullet that renders on two lines may be rewritten with more
words or fewer, as long as it still renders on two lines.

So: measure how wide a line is in this resume's own layout, and count lines
as `ceil(visible characters / width)`.

The width is read off the compiled master (`base/resume.pdf`): pypdf hands
back the text one rendered line at a time, so the wrapped lines *are* the
measurement. The 90th percentile of the long lines is the wrap width —
the longest lines are the ones the paragraph filled completely. It is
cached next to the queue, keyed by the PDF's modification time, and falls
back to a constant when there is no PDF to read.
"""

from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path
from typing import Optional

import paths

ROOT = Path(__file__).resolve().parent.parent
BASE_PDF = paths.BASE / "resume.pdf"
CACHE = Path(os.environ.get("AUTOPILOT_LINE_CACHE", paths.DATA / "line_width.json"))

# Measured on the master this project was built around; only used when the
# compiled master cannot be read.
DEFAULT_CHARS_PER_LINE = 105

# Lines shorter than this are headings, dates and the tails of paragraphs,
# not evidence of how wide a full line is.
MIN_MEASURED = 60

# A line that comes back a few characters over its budget is the estimate
# being an estimate, not a line more on the page.
SLACK = 6

_MACROS_WITH_TEXT = (
    r"\href", r"\textbf", r"\textit", r"\underline", r"\emph", r"\normalsize",
    r"\small", r"\large", r"\texorpdfstring",
)


def _strip_href(text: str) -> str:
    """`\\href{url}{shown}` prints `shown`; the URL costs no width."""
    out, i = [], 0
    while i < len(text):
        at = text.find(r"\href", i)
        if at == -1:
            out.append(text[i:])
            break
        out.append(text[i:at])
        j = text.find("{", at)
        if j == -1:
            out.append(text[at:])
            break
        url_end = _matching(text, j)
        if url_end is None or url_end + 1 >= len(text) or text[url_end + 1] != "{":
            out.append(text[at:j])
            i = j
            continue
        shown_end = _matching(text, url_end + 1)
        if shown_end is None:
            out.append(text[at:])
            break
        out.append(text[url_end + 2:shown_end])
        i = shown_end + 1
    return "".join(out)


def _matching(text: str, open_at: int) -> Optional[int]:
    depth = 0
    for i in range(open_at, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return i
    return None


def visible(fragment: str) -> str:
    """What a reader sees: macros dropped, their text kept, URLs not counted."""
    text = _strip_href(fragment)
    text = re.sub(r"\\color\s*\{[^}]*\}", "", text)
    text = re.sub(r"\\(?:" + "|".join(m[1:] for m in _MACROS_WITH_TEXT) + r")\b", "", text)
    text = re.sub(r"\\[a-zA-Z]+\s*", "", text)
    text = text.replace("{", "").replace("}", "").replace("\\", "")
    text = re.sub(r"[ \t\n]+", " ", text)
    return text.strip()


def _measure(pdf_path: Path) -> Optional[int]:
    try:
        from pypdf import PdfReader
    except ImportError:
        return None
    try:
        text = "\n".join(page.extract_text() or "" for page in PdfReader(str(pdf_path)).pages)
    except Exception:  # noqa: BLE001 - an unreadable PDF is just no measurement
        return None
    long_lines = sorted(len(line.strip()) for line in text.splitlines()
                        if len(line.strip()) >= MIN_MEASURED)
    if len(long_lines) < 5:
        return None
    return long_lines[int(len(long_lines) * 0.9) - 1]


def chars_per_line(pdf_path: Optional[Path] = None) -> int:
    """How many characters one printed line of this resume holds."""
    override = os.environ.get("AUTOPILOT_CHARS_PER_LINE", "").strip()
    if override.isdigit():
        return int(override)
    pdf_path = pdf_path or BASE_PDF
    stamp = str(pdf_path.stat().st_mtime_ns) if pdf_path.exists() else ""
    if CACHE.exists():
        try:
            cached = json.loads(CACHE.read_text())
            if cached.get("stamp") == stamp and cached.get("chars_per_line"):
                return int(cached["chars_per_line"])
        except (json.JSONDecodeError, ValueError):
            pass
    width = _measure(pdf_path) if pdf_path.exists() else None
    width = width or DEFAULT_CHARS_PER_LINE
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps({"stamp": stamp, "chars_per_line": width}) + "\n")
    except OSError:
        pass
    return width


def line_count(fragment: str, width: Optional[int] = None) -> int:
    """Printed lines a bullet takes, from its visible characters."""
    width = width or chars_per_line()
    length = len(visible(fragment))
    return max(1, math.ceil(max(length - SLACK, 1) / width))


def budget(fragment: str, width: Optional[int] = None) -> tuple[int, int, int]:
    """(lines, most characters that still fit, what it uses now)."""
    width = width or chars_per_line()
    length = len(visible(fragment))
    lines = line_count(fragment, width)
    return lines, lines * width + SLACK, length


def room(fragment: str, width: Optional[int] = None) -> tuple[int, int, int]:
    """(lines, characters that fit, characters used) - the room rounded down.

    `budget` adds `SLACK` on top of the rectangle, which is the tolerance the
    checker judges by. What the model is *told* has to lean the other way: a
    line is only approximately a number of characters, the last line of a
    wrapped item is partly filled, and a cap that is six characters generous
    is a cap that sometimes costs an attempt. So the room offered is the bare
    rectangle, `lines * width`, and never less than what the item already
    uses - a master bullet sitting over the rectangle has no room rather than
    a negative amount of it.
    """
    width = width or chars_per_line()
    length = len(visible(fragment))
    lines = line_count(fragment, width)
    return lines, max(length, lines * width), length
