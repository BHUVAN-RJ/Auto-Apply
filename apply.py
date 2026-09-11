"""Checkpoint 2: fill approved applications, then stop.

Separate from pipeline.py on purpose. That one runs unattended over the queue;
this one drives a visible browser on your machine and only ever touches jobs
you have already approved at checkpoint 1.

    python apply.py            # fill every approved job
    python apply.py <job_id>   # fill one
"""

from __future__ import annotations

import sys
from pathlib import Path

from archive import store
from browser import fill as filler
from server import queue
from server.models import Job, Status

ROOT = Path(__file__).resolve().parent


def fill_one(job: Job) -> bool:
    """Fill one approved application and halt at the screenshot."""
    if job.status != Status.APPROVED:
        print(f"  skipped: status is {job.status.value}, not approved", file=sys.stderr)
        return False
    if not job.app_dir:
        print("  skipped: no application folder", file=sys.stderr)
        return False

    app_dir = Path(job.app_dir)
    resume = app_dir / "resume.pdf"
    if not resume.exists():
        print(f"  skipped: {resume} is missing", file=sys.stderr)
        return False

    queue.update(job.id, status=Status.FILLING)
    store.set_status(app_dir, Status.FILLING)

    try:
        result = filler.fill(job.url, resume, app_dir / "fill_screenshot.png")
    except Exception as exc:  # noqa: BLE001 - one bad form must not stop the batch
        detail = f"{type(exc).__name__}: {exc}"
        print(f"  failed: {detail}", file=sys.stderr)
        queue.update(job.id, status=Status.FAILED, error=detail)
        store.set_status(app_dir, Status.FAILED, detail)
        return False

    note = f"{result.steps} steps"
    if result.blocked_attempts:
        note += f"; blocked {result.blocked_attempts} submit attempt(s)"
    if not (app_dir / "fill_notes.md").exists():
        store.write(
            app_dir,
            "fill_notes.md",
            f"# Fill notes\n\n_{note}_\n\n{result.notes}\n",
        )

    # FILLED is terminal for the agent. Only a human moves it to SUBMITTED.
    store.set_status(app_dir, Status.FILLED, note)
    queue.update(job.id, status=Status.FILLED)
    print(f"  filled, {note}. Review it at http://127.0.0.1:8787")
    return True


def main(argv: list[str]) -> int:
    if len(argv) > 1:
        job = queue.get(argv[1])
        if job is None:
            print(f"unknown job id {argv[1]}", file=sys.stderr)
            return 1
        jobs = [job]
    else:
        jobs = [j for j in queue.all_jobs() if j.status == Status.APPROVED]

    if not jobs:
        print("nothing approved. Approve something at http://127.0.0.1:8787 first")
        return 0

    failures = 0
    for job in jobs:
        print(f"{job.id}  {job.title or job.url}")
        if not fill_one(job):
            failures += 1

    print(f"\n{len(jobs) - failures}/{len(jobs)} filled. None submitted — that is yours.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
