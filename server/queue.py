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


class Ambiguous(LookupError):
    """This short id names more than one job, so nobody may act on it.

    `Job.id` is four hex characters of sha256 over the URL (`models.url_hash`),
    which is 65,536 ids; at a few hundred rows a collision is likelier than
    not, and two Twitch postings really did both hash to `96f2`
    (KNOWN_ISSUES #1). Returning the first match meant the newer job had no
    detail page and, worse, that a reject, a fill or a submitted mark aimed
    at one job could land on the other: `POST /review/e5b0/submitted`
    answered 409 three times because `e5b0` is a skipped Dayforce row *and* a
    queued Zoox row, and `get` kept handing back the decided one.

    Refusing is the whole fix. The ids themselves are not lengthened and
    nothing on disk is migrated - the folder names, thread files and `#<id>`
    links keep the display id for good - but an id that cannot name one job
    is not allowed to stand in for one. The candidates travel with the error
    so a page can say which jobs it is between instead of guessing.
    """

    def __init__(self, job_id: str, jobs: list[Job]) -> None:
        self.job_id = job_id
        self.jobs = jobs
        super().__init__(
            f"job id {job_id} names {len(jobs)} jobs: "
            + "; ".join(f"{j.title or 'untitled'} at {j.company or 'unknown'} ({j.url})"
                        for j in jobs)
        )

    def candidates(self) -> list[dict]:
        """What the page shows so a person can tell the jobs apart."""
        return [{"id": j.id, "url": j.url, "title": j.title, "company": j.company,
                 "status": j.status.value, "added_at": j.added_at} for j in self.jobs]


def rows_for(job_id: str) -> list[Job]:
    """Every row carrying this short id - normally one, sometimes two."""
    return [j for j in all_jobs() if j.id == job_id]


def get(job_id: str) -> Optional[Job]:
    """The one job with this id, None if there is none - never a guess.

    Raises `Ambiguous` when the id names more than one row; see that class
    for why guessing was the data-loss bug.
    """
    rows = rows_for(job_id)
    if len(rows) > 1:
        raise Ambiguous(job_id, rows)
    return rows[0] if rows else None


def update(job_id: str, **fields) -> Optional[Job]:
    """Patch fields on one job. Read-modify-write happens inside the lock.

    An ambiguous id is refused here too, and for a harder reason than in
    `get`: this is the write. Patching the first of two colliding rows sets
    a status, a reject reason or an `app_dir` on a job nobody was looking
    at, and nothing afterwards says it happened.
    """
    with _locked():
        jobs = _read_unlocked()
        matches = [index for index, job in enumerate(jobs) if job.id == job_id]
        if len(matches) > 1:
            raise Ambiguous(job_id, [jobs[index] for index in matches])
        if not matches:
            return None
        index = matches[0]
        jobs[index] = jobs[index].model_copy(update=fields)
        _write_unlocked(jobs)
        return jobs[index]


def update_row(job: Job, **fields) -> Optional[Job]:
    """Patch the one row with this exact URL.

    The way to write to a job whose short id names two rows: the ids collide
    because the URLs differ, so the URL is exact where the id is not. Used
    where the caller has already worked out *which* row it means - the page
    sent the posting's own URL - and `update` would therefore refuse the
    write it is sure about.
    """
    with _locked():
        jobs = _read_unlocked()
        for index, existing in enumerate(jobs):
            if existing.url == job.url:
                jobs[index] = existing.model_copy(update=fields)
                _write_unlocked(jobs)
                return jobs[index]
        return None


def pending() -> list[Job]:
    """Jobs the pipeline has not started yet, oldest first."""
    return [j for j in all_jobs() if j.status == Status.QUEUED]
