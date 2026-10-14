"""Reading the jobs this install already knows about. Read-only, always.

The queue (`data/queue.json`) is the source of truth for a job's status.
The archive (`applications/<folder>/`) is where the posting itself, the
status history and the folder's own copy of the job record live, and it
keeps rows the queue no longer carries. `applications/index.csv` is a
derived view of the archive and is read for the folders whose `job.json`
is missing.

Nothing here writes, moves or deletes anything: this package builds an
index beside the data and never inside it.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

import paths

from . import keys

# The statuses a verdict cares about, in the order "what happened to this
# job?" should be answered in - the row that got furthest is the answer,
# not whichever row a loop reached first. This is `seen.APPLIED_FIRST`,
# widened to the whole lifecycle so a submission always outranks a
# duplicate added this morning.
FURTHEST_FIRST = [
    "submitted", "filled", "filling", "approved", "tailoring",
    "awaiting_review", "queued", "failed", "skipped",
]

# The posting text starts after the header block.
_BODY = "\n---\n"
_HEADER = re.compile(r"^\*\*(?P<name>[A-Za-z ]+):\*\*\s*(?P<value>.+)$", re.M)
_HEADER_BYTES = 4096


@dataclass
class KnownJob:
    """One job this install knows about, with every key it answers to."""

    key: str
    display_id: str
    url: str
    canonical: str
    title: str = ""
    company: str = ""
    location: str = ""
    status: str = ""
    added_at: str = ""
    decided_at: str = ""
    folder: str = ""
    origin: str = "queue"
    reject: str = ""
    simhash: int = 0
    text_fp: str = ""
    also: list[str] = field(default_factory=list)

    @property
    def row_id(self) -> str:
        """Primary key of the index.

        Deliberately the long key *and* the short display id: two rows that
        canonicalise to one URL are kept apart rather than merged, because
        merging two jobs into one row is the shape of the bug this package
        exists to make visible (KNOWN_ISSUES #1).
        """
        return f"{self.key}:{self.display_id}"

    @property
    def rank(self) -> int:
        try:
            return FURTHEST_FIRST.index(self.status)
        except ValueError:
            return len(FURTHEST_FIRST)

    def identity_keys(self) -> list[tuple[str, str]]:
        """Every (kind, value) this job can be found by."""
        found = [("url", self.canonical)]
        ats = keys.ats_identity(self.url)
        if ats:
            found.append(("ats", ":".join(ats)))
        jr = keys.jobright_id(self.url)
        if jr:
            found.append(("jobright", jr))
        company = keys.norm_company(self.company)
        title = keys.norm_title(self.title, self.company)
        if company:
            found.append(("company", company))
            if title:
                found.append(("pair", f"{company}|{title}"))
        return found


# Where the data is, when something has moved it. This service may not
# import the modules that *write* the queue and the archive (invariant 8),
# so it declares the seam and the server fills it in: `server/seen.py`
# registers a reader for each on import. The reason it matters at all is
# that a lookup against the wrong queue answers CLEAR, and a false CLEAR
# is the single failure this service exists to prevent - so the index has
# to follow the queue wherever the app has actually put it (a test, a
# second server started from another home - see CLAUDE.md on two servers
# and two data folders).
PATH_HOOKS: dict[str, Callable[[], Path]] = {}


def queue_path() -> Path:
    """Resolved per call: a module-level default freezes at import, which
    has bitten this repository twice (see CLAUDE.md).

    An explicit `AUTOPILOT_QUEUE` wins, since that is what an override is
    for; then whatever the app registered; then the data directory.
    """
    named = os.environ.get("AUTOPILOT_QUEUE")
    if named:
        return Path(named)
    hook = PATH_HOOKS.get("queue")
    if hook is not None:
        try:
            return Path(hook())
        except Exception:                            # noqa: BLE001 - fall through to the default
            pass
    return paths.DATA / "queue.json"


def applications_path() -> Path:
    """The archive, by the same three steps."""
    named = os.environ.get("AUTOPILOT_APPLICATIONS")
    if named:
        return Path(named)
    hook = PATH_HOOKS.get("applications")
    if hook is not None:
        try:
            return Path(hook())
        except Exception:                            # noqa: BLE001
            pass
    return Path(paths.APPLICATIONS)


def _fingerprint(path: Path) -> str:
    try:
        stat = path.stat()
    except OSError:
        return "missing"
    return f"{stat.st_mtime_ns}:{stat.st_size}"


def fingerprints() -> dict[str, str]:
    """What the index was built from, cheaply enough to check per lookup.

    The queue file by mtime and size; the applications directory by the
    name and mtime of every entry in it, digested. A folder whose contents
    change bumps its own mtime, so a new posting, a new status or a
    re-tailor all show up here.
    """
    applications = applications_path()
    entries: list[str] = []
    try:
        with os.scandir(applications) as scan:
            for entry in scan:
                if entry.name.startswith("."):
                    continue
                try:
                    entries.append(f"{entry.name}:{entry.stat().st_mtime_ns}")
                except OSError:
                    entries.append(entry.name)
    except OSError:
        entries = ["missing"]
    digest = hashlib.sha256("\n".join(sorted(entries)).encode()).hexdigest()[:16]
    return {
        "queue": _fingerprint(queue_path()),
        "applications": f"{len(entries)}:{digest}",
    }


def _header(folder: Path) -> tuple[dict[str, str], str]:
    """The `**Company:**` / `**Location:**` block of `posting.md`.

    Only the first few kilobytes are read: the header is at the top and a
    posting can be 140k characters.
    """
    path = folder / "posting.md"
    try:
        with path.open(errors="replace") as handle:
            head = handle.read(_HEADER_BYTES)
    except OSError:
        return {}, "missing"
    fields = {m.group("name").strip().lower(): m.group("value").strip()
              for m in _HEADER.finditer(head.partition(_BODY)[0])}
    return fields, _fingerprint(path)


def posting_text(folder: Path) -> str:
    """The posting itself, without the header this project writes above it."""
    path = folder / "posting.md"
    try:
        return path.read_text(errors="replace").partition(_BODY)[2]
    except OSError:
        return ""


def _status(folder: Path) -> tuple[str, str]:
    """(status, when it reached that status) off `status.json`'s history."""
    path = folder / "status.json"
    try:
        state = json.loads(path.read_text())
    except (OSError, ValueError):
        return "", ""
    status = state.get("status") or ""
    when = ""
    for entry in state.get("history") or []:
        if entry.get("status") == status:
            when = entry.get("at") or ""
            break
    return status, when


def _row(url: str, display_id: str, **rest) -> KnownJob:
    return KnownJob(key=keys.identity_key(url), display_id=display_id,
                    url=url, canonical=keys.canonical(url), **rest)


def _queue_rows() -> list[KnownJob]:
    path = queue_path()
    try:
        raw = json.loads(path.read_text() or "[]")
    except (OSError, ValueError):
        return []
    rows: list[KnownJob] = []
    for item in raw if isinstance(raw, list) else []:
        url = (item.get("url") or "").strip()
        if not url:
            continue
        folder = item.get("app_dir") or ""
        rows.append(_row(
            url,
            hashlib.sha256(url.encode()).hexdigest()[:4],
            title=item.get("title") or "",
            company=item.get("company") or "",
            status=item.get("status") or "",
            added_at=item.get("added_at") or "",
            folder=folder,
            origin="queue",
            reject=item.get("reject_reason") or "",
        ))
    return rows


def _archive_rows() -> list[KnownJob]:
    """Every application folder, and the index rows whose folder is gone.

    A folder's own `job.json` is preferred over `index.csv`: the index is
    rewritten in place on every status change and one row in the real file
    is corrupt (a URL with a newline in it split it across two records).
    """
    root = applications_path()
    rows: list[KnownJob] = []
    seen_folders: set[str] = set()
    try:
        folders = sorted(entry for entry in root.iterdir() if entry.is_dir())
    except OSError:
        folders = []
    for folder in folders:
        try:
            record = json.loads((folder / "job.json").read_text())
        except (OSError, ValueError):
            continue
        url = (record.get("url") or "").strip()
        if not url:
            continue
        seen_folders.add(folder.name)
        status, when = _status(folder)
        rows.append(_row(
            url,
            hashlib.sha256(url.encode()).hexdigest()[:4],
            title=record.get("title") or "",
            company=record.get("company") or "",
            status=status or record.get("status") or "",
            added_at=record.get("added_at") or "",
            decided_at=when,
            folder=str(folder),
            origin="archive",
        ))
    index = root / "index.csv"
    if index.exists():
        try:
            records = list(csv.DictReader(index.open(newline="")))
        except OSError:
            records = []
        for record in records:
            url = (record.get("url") or "").strip()
            folder = record.get("folder") or ""
            if not url or "://" not in url or folder in seen_folders:
                continue  # corrupt row, or a folder already read in full
            rows.append(_row(
                url,
                (record.get("job_id") or "")[:4],
                title=record.get("title") or "",
                company=record.get("company") or "",
                status=record.get("status") or "",
                added_at=record.get("created_at") or "",
                origin="index",
            ))
    return rows


def _merge(rows: Iterable[KnownJob]) -> list[KnownJob]:
    """One row per (long key, display id), the queue's copy winning.

    A job in the queue and in the archive is one job; the queue holds its
    status and the archive holds its folder, so the two are folded
    together rather than reported twice.
    """
    merged: dict[str, KnownJob] = {}
    for row in rows:
        kept = merged.get(row.row_id)
        if kept is None:
            merged[row.row_id] = row
            continue
        if kept.origin != "queue" and row.origin == "queue":
            row.folder = row.folder or kept.folder
            row.decided_at = row.decided_at or kept.decided_at
            merged[row.row_id] = row
            continue
        kept.folder = kept.folder or row.folder
        kept.decided_at = kept.decided_at or row.decided_at
        kept.company = kept.company or row.company
        kept.title = kept.title or row.title
        kept.added_at = kept.added_at or row.added_at
    return list(merged.values())


def collect(simhash_for: Optional[Callable[[Path, str], tuple[int, str]]] = None) -> list[KnownJob]:
    """Every job this install knows about, with its keys filled in.

    `simhash_for(folder, fingerprint)` is the cache hook: it is handed the
    folder and the `posting.md` fingerprint and answers the simhash, so a
    refresh re-reads only the postings that changed. Hashing all 380
    postings from scratch takes about 4.5 s; everything else here takes
    about 0.3 s.
    """
    rows = _merge(_queue_rows() + _archive_rows())
    for row in rows:
        folder = Path(row.folder) if row.folder else None
        if not folder or not folder.is_dir():
            row.folder = ""
            continue
        fields, fingerprint = _header(folder)
        row.text_fp = fingerprint
        row.company = row.company or fields.get("company", "")
        row.location = row.location or fields.get("location", "")
        if not row.decided_at:
            row.decided_at = _status(folder)[1]
        if simhash_for:
            row.simhash, row.text_fp = simhash_for(folder, fingerprint)
    for row in rows:
        if not row.company:
            row.company = keys.company_from_title(row.title)
        row.decided_at = row.decided_at or row.added_at
    return rows
