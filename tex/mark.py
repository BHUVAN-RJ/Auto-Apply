"""Colour the tailored resume's changes so the review page can show them in
the PDF itself, next to the plain one.

Two variants of the tailored source, both derived, never sent to the model:

- ``changes``: the tailored text, with every changed span in green.
- ``both``: the same, with what the master said struck through in red just
  before each green span.

Lines are paired with difflib. Within a changed pair the common prefix and
suffix are kept and only the middle is wrapped, so a bullet that lost one
word shows one word. The wrap has to land inside the line's own braces
(``\\resumeItem{\\normalsize{...}}``): a span that would split a brace group
is widened to the nearest balanced one, and a line that cannot be wrapped
at all is left plain rather than broken. Nothing above ``\\begin{document}``
is touched except the two lines added at its end for the colours and the
strike-out.
"""
from __future__ import annotations

import difflib
import re

MODES = ("changes", "both")

PREAMBLE = (
    "\\usepackage[normalem]{ulem}\n"
    "\\definecolor{tailoradd}{rgb}{0,0.55,0.15}\n"
    "\\definecolor{tailordel}{rgb}{0.8,0.1,0.1}\n"
)

# A `{\cmd ` group holding a whole line's text.
_GROUP = re.compile(r"^(\s*\{\\[A-Za-z]+\s+)(.*?)(\}\s*)$")


def _balanced(text: str) -> bool:
    depth = 0
    escaped = False
    for char in text:
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def _add(text: str) -> str:
    return "{\\color{tailoradd}" + text + "}"


def _del(text: str) -> str:
    return "{\\color{tailordel}\\sout{" + text + "}}"


def _split(old: str, new: str) -> tuple[str, str, str, str] | None:
    """(prefix, old middle, new middle, suffix), widened to word boundaries
    and to balanced braces; None when no balanced split exists."""
    lo = 0
    while lo < min(len(old), len(new)) and old[lo] == new[lo]:
        lo += 1
    hi = 0
    while (hi < min(len(old), len(new)) - lo
           and old[-1 - hi] == new[-1 - hi]):
        hi += 1
    # Back off to a space or a brace so a changed word is coloured whole.
    while lo > 0 and old[lo - 1] not in " \t{":
        lo -= 1
    while hi > 0 and old[len(old) - hi] not in " \t}":
        hi -= 1
    # Widen until both middles are brace-balanced, one space at a time.
    while True:
        old_mid = old[lo:len(old) - hi]
        new_mid = new[lo:len(new) - hi]
        if _balanced(old_mid) and _balanced(new_mid):
            if _OPENER.match(old_mid.lstrip()) or _OPENER.match(new_mid.lstrip()):
                # The span swallowed the line's own command; colour it whole.
                return None
            return old[:lo], old_mid, new_mid, old[len(old) - hi:]
        moved = False
        if lo > 0:
            lo = old.rfind(" ", 0, lo - 1) + 1 if lo > 1 else 0
            moved = True
        elif hi > 0:
            cut = old.find(" ", len(old) - hi + 1)
            hi = len(old) - cut if cut != -1 else 0
            moved = True
        if not moved:
            return None


_OPENER = re.compile(r"\\[A-Za-z]+\{")


def _whole(line: str, wrap) -> str | None:
    """The line with its text coloured whole, or None when its shape is
    not one we know how to get inside."""
    stripped = line.rstrip()
    indent = len(line) - len(line.lstrip())
    # Step through `\cmd{` openers from the outside in; the innermost one
    # whose group is balanced and closes at the end of the line is where
    # the text lives. `\href{url}{...}` is an opener too, but its group
    # closes early, so it is passed over.
    openers = []
    pos = indent
    while True:
        match = _OPENER.match(stripped, pos)
        if not match:
            break
        openers.append(match.end())
        pos = match.end()
    for end in reversed(openers):
        depth = len(openers[:openers.index(end) + 1])
        inner = stripped[end:]
        if inner.endswith("}" * depth) and _balanced(inner[:-depth]):
            return stripped[:end] + wrap(inner[:-depth]) + inner[-depth:]
    match = _GROUP.match(line)
    if match and _balanced(match.group(2)):
        return match.group(1) + wrap(match.group(2)) + match.group(3)
    return None


def _pair(old: str, new: str, mode: str) -> str:
    parts = _split(old, new)
    if parts is None:
        whole = _whole(new, _add)
        if whole is None:
            return new
        if mode == "both":
            struck = _whole(old, _del)
            return (struck + "\n" if struck else "") + whole
        return whole
    prefix, old_mid, new_mid, suffix = parts
    mid = _add(new_mid) if new_mid else ""
    if mode == "both" and old_mid:
        mid = _del(old_mid) + " " + mid
    return prefix + mid + suffix


def mark(original: str, tailored: str, mode: str) -> str:
    """The tailored source with its changes coloured, per ``mode``."""
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}")
    head, sep, body = tailored.partition("\\begin{document}")
    if not sep:
        return tailored
    orig_body = original.partition("\\begin{document}")[2]
    old_lines = orig_body.splitlines()
    new_lines = body.splitlines()
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    opcodes = matcher.get_opcodes()
    # Match every changed new line to the changed old line it most
    # resembles, across the whole body, not block by block: reordering is
    # legal, so a bullet moved unchanged stays plain, a moved and edited
    # one shows its edit, and only a bullet with no counterpart is
    # coloured whole.
    changed_old = [i for tag, i1, i2, _, _ in opcodes if tag != "equal" for i in range(i1, i2)]
    changed_new = [j for tag, _, _, j1, j2 in opcodes if tag != "equal" for j in range(j1, j2)]
    partner: dict[int, int] = {}
    taken: set[int] = set()
    for j in changed_new:
        new = new_lines[j]
        best = max(
            ((1.0 if old_lines[i] == new else difflib.SequenceMatcher(None, old_lines[i], new).ratio(), -i)
             for i in changed_old if i not in taken),
            default=(0.0, 1))
        if best[0] >= 0.6:
            partner[j] = -best[1]
            taken.add(-best[1])
    out: list[str] = []
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "equal":
            out.extend(new_lines[j1:j2])
            continue
        for j in range(j1, j2):
            new = new_lines[j]
            if j in partner:
                old = old_lines[partner[j]]
                out.append(new if old == new else _pair(old, new, mode))
            else:
                out.append(_whole(new, _add) or new)
        if mode == "both":
            for i in range(i1, i2):
                if i not in taken:
                    struck = _whole(old_lines[i], _del)
                    if struck:
                        out.append(struck)
    return head + PREAMBLE + sep + "\n".join(out) + ("\n" if body.endswith("\n") else "")
