"""Every system prompt, and the person's own version of each.

The stock text stays where it always was: the `*_rules.md` files beside
the code and the reply-format constants inside it. Nothing reads those
directly any more; every call site asks `text(name)`, which returns the
person's edit when there is one and the stock text otherwise.

An edit is a file under `paths.PROMPTS` (the data directory, never the
clone), so `git pull` cannot collide with it. Beside it, `.base/<name>.md`
keeps the stock text the edit was made against. When an update changes the
stock text, `sync()` three-way merges the person's edit onto it with
`git merge-file`: their changes kept, the update's changes added. A merge
that conflicts leaves their edit working as it was and puts the marked-up
result in `.conflict/<name>.md` for the Prompts tab to resolve.

Every save and every reset keeps the text it replaced in
`.history/<name>/`, so nothing the person wrote is ever lost to a click.

The invariants are not in any of these texts: never submitting, the visa
guard, the checker's sections, lines and links are all enforced in code,
so a prompt can be changed freely without reaching them.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import paths

# Where edits live. Resolved per call: tests point it at a temporary folder.
OVERLAY: Optional[Path] = None

HISTORY_KEEP = 30

FORMAT_NOTE = ("The code reads the reply in this shape. Reword it freely; change the "
               "shape and the replies stop parsing.")


@dataclass(frozen=True)
class Prompt:
    name: str
    title: str
    group: str
    note: str
    stock: Callable[[], str]


def _file(module: str, attr: str) -> Callable[[], str]:
    # The module attribute is read at call time, so a test that points
    # RULES at a temporary file is still honoured.
    return lambda: getattr(importlib.import_module(module), attr).read_text()


def _const(module: str, attr: str) -> Callable[[], str]:
    return lambda: getattr(importlib.import_module(module), attr)


def _workshop_file(name: str) -> Callable[[], str]:
    return lambda: (Path(__file__).resolve().parent / name).read_text()


CATALOG: tuple[Prompt, ...] = (
    Prompt("tailor", "Resume tailoring", "Tailoring",
           "How the resume is rewritten for a posting. The checker still holds the "
           "sections, the counts, the links and the printed lines.",
           _file("tailor.tailor", "RULES")),
    Prompt("tailor.format", "Resume reply format", "Tailoring", FORMAT_NOTE,
           _const("tailor.tailor", "REPLY_FORMAT")),
    Prompt("cover", "Cover letter", "Tailoring", "How the cover letter is written.",
           _file("tailor.cover", "RULES")),
    Prompt("cover.format", "Cover letter reply format", "Tailoring", FORMAT_NOTE,
           _const("tailor.cover", "REPLY_FORMAT")),
    Prompt("answers", "Form answers", "Tailoring",
           "How the open questions on a form are answered. Visa questions are "
           "refused in code whatever this says.",
           _file("tailor.answers", "RULES")),
    Prompt("answers.format", "Form answer reply format", "Tailoring", FORMAT_NOTE,
           _const("tailor.answers", "REPLY_FORMAT")),
    Prompt("screen", "Posting screen", "Screening",
           "What counts as a reject, a caution or fine when a posting opens. The US "
           "location and graduation rules are also enforced in code.",
           _file("tailor.screen", "RULES")),
    Prompt("screen.format", "Screen reply format", "Screening", FORMAT_NOTE,
           _const("tailor.screen", "REPLY_FORMAT")),
    Prompt("ats", "Application systems", "Form",
           "Notes per application system (Oracle, Greenhouse, Ashby, Workday, Lever).",
           _file("browser.ats", "RULES")),
    Prompt("interview", "Profile interview", "Profile",
           "How the interviewer asks about each experience.",
           _file("tailor.interview", "RULES")),
    Prompt("interview.format", "Interview reply format", "Profile", FORMAT_NOTE,
           _const("tailor.interview", "REPLY_FORMAT")),
    Prompt("interview.github", "Interview: GitHub projects", "Profile",
           "Added when the experience came from a GitHub scan.",
           _const("tailor.interview", "GITHUB_NOTE")),
    Prompt("interview.extract", "Interview: find experiences", "Profile", FORMAT_NOTE,
           _const("tailor.interview", "EXTRACT_PROMPT")),
    Prompt("interview.setup", "Interview: confirm the list", "Profile", FORMAT_NOTE,
           _const("tailor.interview", "SETUP_PROMPT")),
    Prompt("interview.grade", "Interview: grade an answer", "Profile", FORMAT_NOTE,
           _const("tailor.interview", "GRADE_PROMPT")),
    Prompt("interview.update", "Interview: update documents", "Profile", "",
           _const("tailor.interview", "UPDATE_PROMPT")),
    Prompt("interview.write_main", "Interview: write the story", "Profile", "",
           _const("tailor.interview", "WRITE_MAIN_PROMPT")),
    Prompt("interview.rewrite_main", "Interview: rewrite the story", "Profile", "",
           _const("tailor.interview", "REWRITE_MAIN_PROMPT")),
    Prompt("story", "Story documents", "Profile",
           "How tailor.md and star.md are written from a story.",
           _file("tailor.interview", "STORY_RULES")),
    Prompt("facts.contact", "Facts: contacts from the resume", "Profile", FORMAT_NOTE,
           _const("tailor.facts", "CONTACT_PROMPT")),
    Prompt("facts.normalise", "Facts: one line per answer", "Profile", "",
           _const("tailor.facts", "NORMALISE_PROMPT")),
    Prompt("profile.pick", "Stories for a posting", "Profile",
           "Picks which stories a posting is tailored with. {n} is the number to pick.",
           _const("tailor.profile", "PICK_PROMPT")),
    Prompt("profile.derive", "Facts from the resume", "Profile", "",
           _const("tailor.profile", "DERIVE_PROMPT")),
    Prompt("github", "GitHub project reader", "Profile",
           "How a repository becomes a project scaffold and its questions.",
           _file("tailor.github", "RULES")),
    Prompt("workshop", "Workshop", "Workshop",
           "The assistant that edits these prompts for you. It can edit itself too.",
           _workshop_file("workshop_rules.md")),
    Prompt("workshop.pick", "Workshop: which prompts", "Workshop",
           "Routes a request to the prompts it is about. {n} is how many at most.",
           _const("tailor.workshop", "PICK_PROMPT")),
    Prompt("learn", "Learning from your feedback", "Workshop",
           "How your re-tailor requests are turned into suggested prompt changes.",
           _workshop_file("learn_rules.md")),
)

BY_NAME = {p.name: p for p in CATALOG}


class PromptError(KeyError):
    pass


def get(name: str) -> Prompt:
    try:
        return BY_NAME[name]
    except KeyError:
        raise PromptError(f"no prompt named {name!r}") from None


# ------------------------------------------------------------ storage --


def overlay_dir() -> Path:
    return OVERLAY or Path(os.environ.get("AUTOPILOT_PROMPTS") or paths.PROMPTS)


def _edit_path(name: str) -> Path:
    return overlay_dir() / f"{name}.md"


def _base_path(name: str) -> Path:
    return overlay_dir() / ".base" / f"{name}.md"


def _conflict_path(name: str) -> Path:
    return overlay_dir() / ".conflict" / f"{name}.md"


def _history_dir(name: str) -> Path:
    return overlay_dir() / ".history" / name


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _read(path: Path) -> Optional[str]:
    try:
        return path.read_text()
    except FileNotFoundError:
        return None


def stock(name: str) -> str:
    return get(name).stock()


def edited(name: str) -> Optional[str]:
    return _read(_edit_path(get(name).name))


def text(name: str) -> str:
    """What the model is sent: the person's version, else the stock one."""
    mine = edited(name)
    return mine if mine is not None else stock(name)


def _remember(name: str, previous: str, why: str) -> None:
    folder = _history_dir(name)
    _write_atomic(folder / f"{_now()}_{why}.md", previous)
    kept = sorted(folder.glob("*.md"))
    for old in kept[:-HISTORY_KEEP]:
        old.unlink(missing_ok=True)


def save(name: str, new_text: str, why: str = "edit") -> dict:
    """The person's version of a prompt. Saving the stock text back is a
    reset. Clears a pending update conflict: whatever is saved is the
    resolution, made against the current stock text."""
    get(name)
    current = text(name)
    if new_text == current and _read(_conflict_path(name)) is None:
        return detail(name)
    _remember(name, current, why)
    base = stock(name)
    if new_text == base:
        _clear(name)
    else:
        _write_atomic(_edit_path(name), new_text)
        _write_atomic(_base_path(name), base)
        _conflict_path(name).unlink(missing_ok=True)
    return detail(name)


def _clear(name: str) -> None:
    for path in (_edit_path(name), _base_path(name), _conflict_path(name)):
        path.unlink(missing_ok=True)


def reset(name: str) -> dict:
    get(name)
    if edited(name) is not None:
        _remember(name, text(name), "reset")
    _clear(name)
    return detail(name)


def history(name: str) -> list[dict]:
    folder = _history_dir(get(name).name)
    out = []
    for path in sorted(folder.glob("*.md"), reverse=True):
        stamp, _, why = path.stem.partition("_")
        out.append({"version": path.stem, "when": stamp, "why": why or "edit"})
    return out


def restore(name: str, version: str) -> dict:
    path = _history_dir(get(name).name) / f"{Path(version).name}.md"
    old = _read(path)
    if old is None:
        raise PromptError(f"no version {version!r} of {name!r}")
    return save(name, old, why="restore")


# -------------------------------------------------------------- merge --


def merge3(mine: str, base: str, theirs: str) -> tuple[str, bool]:
    """(merged, clean). `git merge-file`: the person's edit and the new
    stock text, both against the stock text the edit was made from."""
    with tempfile.TemporaryDirectory(prefix="autopilot-merge-") as tmp:
        folder = Path(tmp)
        files = []
        for label, body in (("yours", mine), ("before", base), ("update", theirs)):
            path = folder / label
            path.write_text(body)
            files.append(str(path))
        try:
            proc = subprocess.run(
                ["git", "merge-file", "-p", "-L", "your version", "-L", "stock before",
                 "-L", "update", *files],
                capture_output=True, text=True, timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            return mine, False
        # Exit status is the number of conflicts; negative is an error.
        if proc.returncode < 0 or proc.returncode > 127:
            return mine, False
        return proc.stdout, proc.returncode == 0


def sync() -> list[dict]:
    """Bring every edit onto the current stock text. Run at startup, so the
    first start after an update merges. Returns what happened, per prompt."""
    report = []
    for prompt in CATALOG:
        mine = edited(prompt.name)
        if mine is None:
            continue
        base = _read(_base_path(prompt.name))
        new = stock(prompt.name)
        if base is None:
            # An edit without its base (copied in by hand): take today's
            # stock as the base, so the next update can merge.
            _write_atomic(_base_path(prompt.name), new)
            continue
        if base == new:
            continue
        merged, clean = merge3(mine, base, new)
        if clean:
            _remember(prompt.name, mine, "before-update")
            _write_atomic(_edit_path(prompt.name), merged)
            _write_atomic(_base_path(prompt.name), new)
            _conflict_path(prompt.name).unlink(missing_ok=True)
            report.append({"name": prompt.name, "merged": True})
        else:
            # Their edit keeps working as it was; the marked-up merge waits.
            _write_atomic(_conflict_path(prompt.name), merged)
            report.append({"name": prompt.name, "merged": False})
    return report


# ------------------------------------------------------------ reading --


def summary(prompt: Prompt) -> dict:
    return {
        "name": prompt.name, "title": prompt.title, "group": prompt.group,
        "note": prompt.note,
        "edited": _edit_path(prompt.name).exists(),
        "conflict": _conflict_path(prompt.name).exists(),
    }


def listing() -> list[dict]:
    return [summary(p) for p in CATALOG]


def detail(name: str) -> dict:
    prompt = get(name)
    return summary(prompt) | {
        "text": text(name),
        "stock": stock(name),
        "conflict_text": _read(_conflict_path(name)),
        "history": history(name),
    }
