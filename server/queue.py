"""Persistent job queue backed by a single JSON file.

The queue file is the seam between the capture half (browser extension) and
the pipeline half (tailor, compile, fill). Both sides touch it only through
this module, which serialises writes with a lock file so a browser POST and a
pipeline update cannot interleave.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

import paths
from .models import Job, Status

ROOT = Path(__file__).resolve().parent.parent
QUEUE_PATH = Path(os.environ.get("AUTOPILOT_QUEUE", paths.DATA / "queue.json"))
LOCK_PATH = QUEUE_PATH.with_suffix(".lock")
LOCK_TIMEOUT = 10.0


@contextmanager
def _locked() -> Iterator[None]:
    """Crude but adequate cross-process lock: exclusive create of a lock file."""
    QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + LOCK_TIMEOUT
    while True:
        try:
            fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            break
        except FileExistsError:
            if time.monotonic() > deadline:
                # A crashed process left the lock behind; take it over.
                LOCK_PATH.unlink(missing_ok=True)
                continue
            time.sleep(0.05)
    try:
        yield
    finally:
        LOCK_PATH.unlink(missing_ok=True)


def _read_unlocked() -> list[Job]:
    if not QUEUE_PATH.exists():
        return []
    raw = json.loads(QUEUE_PATH.read_text() or "[]")
    return [Job(**item) for item in raw]


def _write_unlocked(jobs: list[Job]) -> None:
    QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps([j.model_dump(mode="json") for j in jobs], indent=2)
    # Write to a temp file in the same directory, then rename, so a crash
    # mid-write can never leave a truncated queue.
    fd, tmp = tempfile.mkstemp(dir=QUEUE_PATH.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(payload)
        os.replace(tmp, QUEUE_PATH)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def all_jobs() -> list[Job]:
    with _locked():
        return _read_unlocked()


def add(job: Job) -> tuple[Job, bool]:
    """Append a job. Returns (job, created); a posting already here is never
    duplicated, whether it arrives by the same URL, the same Jobright id, or
    the same job id at the employer's tracking system (`seen.same_job`)."""
    from . import seen  # seen reads the queue; imported here to avoid the cycle

    with _locked():
        jobs = _read_unlocked()
        for existing in jobs:
            if seen.same_job(existing, job):
                return existing, False
        jobs.append(job)
        _write_unlocked(jobs)
        return job, True


def get(job_id: str) -> Optional[Job]:
    return next((j for j in all_jobs() if j.id == job_id), None)


def update(job_id: str, **fields) -> Optional[Job]:
    """Patch fields on one job. Read-modify-write happens inside the lock."""
    with _locked():
        jobs = _read_unlocked()
        for index, job in enumerate(jobs):
            if job.id == job_id:
                jobs[index] = job.model_copy(update=fields)
                _write_unlocked(jobs)
                return jobs[index]
        return None


def pending() -> list[Job]:
    """Jobs the pipeline has not started yet, oldest first."""
    return [j for j in all_jobs() if j.status == Status.QUEUED]
