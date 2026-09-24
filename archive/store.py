"""Immutable per-application archive.

One folder per application attempt, holding everything needed to reconstruct
what was sent and why. The core rule: nothing here is ever overwritten or
deleted. Re-tailoring a job that already has a folder allocates `_v2`, `_v3`,
and so on, so the history of what was tried stays intact.
"""

from __future__ import annotations

import csv
import json
import os
from pathlib import Path
from typing import Optional

import paths
from server.models import Job, Status, utcnow

ROOT = Path(__file__).resolve().parent.parent
APPLICATIONS = Path(os.environ.get("AUTOPILOT_APPLICATIONS", paths.APPLICATIONS))
INDEX_PATH = APPLICATIONS / "index.csv"

INDEX_FIELDS = [
    "folder",
    "job_id",
    "company",
    "title",
    "source",
    "url",
    "created_at",
    "status",
]


class ArchiveError(RuntimeError):
    pass


def create(job: Job) -> Path:
    """Allocate a fresh folder for this job. Never reuses an existing one."""
    APPLICATIONS.mkdir(parents=True, exist_ok=True)
    version = 1
    while True:
        path = APPLICATIONS / job.folder_name(version)
        try:
            path.mkdir(parents=True)
            break
        except FileExistsError:
            version += 1
            if version > 50:
                raise ArchiveError(f"refusing to allocate {job.folder_name(version)}")

    write(path, "job.json", json.dumps(
        job.model_dump(mode="json") | {"id": job.id, "version": version},
        indent=2,
    ))
    _append_index(path, job)
    return path


def write(app_dir: Path, name: str, content: str | bytes) -> Path:
    """Write one artifact. Refuses to clobber an existing file."""
    target = app_dir / name
    if target.exists():
        raise ArchiveError(f"{target} already exists; archive files are immutable")
    mode = "wb" if isinstance(content, bytes) else "w"
    with open(target, mode) as fh:
        fh.write(content)
    return target


def write_or_append(app_dir: Path, name: str, content: str) -> Path:
    """Append to an artifact that legitimately accumulates.

    The immutability rule exists to protect the record of what was sent. A
    reviewer changing their mind is part of that record, so their decisions
    append rather than collide.
    """
    target = app_dir / name
    with open(target, "a") as fh:
        if target.stat().st_size:
            fh.write("\n---\n\n")
        fh.write(content)
    return target


def set_status(app_dir: Path, status: Status, note: Optional[str] = None) -> None:
    """Status is the one mutable file, since it is the folder's lifecycle.

    Every transition is appended to a history list rather than replacing the
    previous value, so the sequence of states remains auditable.
    """
    path = app_dir / "status.json"
    if path.exists():
        state = json.loads(path.read_text())
    else:
        state = {"status": None, "history": []}
    state["status"] = status.value
    state["updated_at"] = utcnow()
    state["history"].append({"status": status.value, "at": utcnow(), "note": note})
    path.write_text(json.dumps(state, indent=2))
    _update_index_status(app_dir.name, status)


def read_status(app_dir: Path) -> Optional[str]:
    path = app_dir / "status.json"
    if not path.exists():
        return None
    return json.loads(path.read_text()).get("status")


def _append_index(app_dir: Path, job: Job) -> None:
    exists = INDEX_PATH.exists()
    with open(INDEX_PATH, "a", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=INDEX_FIELDS)
        if not exists:
            writer.writeheader()
        writer.writerow({
            "folder": app_dir.name,
            "job_id": job.id,
            "company": job.company,
            "title": job.title,
            "source": job.source,
            "url": job.url,
            "created_at": utcnow(),
            "status": Status.TAILORING.value,
        })


def _update_index_status(folder: str, status: Status) -> None:
    """Rewrite the one row whose folder matches. The index is a derived view,
    so unlike the application folders it may be rewritten in place."""
    if not INDEX_PATH.exists():
        return
    rows = list(csv.DictReader(INDEX_PATH.open()))
    for row in rows:
        if row["folder"] == folder:
            row["status"] = status.value
    with open(INDEX_PATH, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=INDEX_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
