"""Phase 2 pipeline: queued job -> tailored, compiled, archived, awaiting review.

This is the half that runs before checkpoint 1. It deliberately stops at
AWAITING_REVIEW; the browser fill loop is a separate phase and only ever runs
against a job a human has approved.

    python pipeline.py            # process every queued job
    python pipeline.py <job_id>   # process one
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

from archive import store
from server import queue
from server.models import Job, Status
from tailor import fetch, tailor
from tex import compile as texc

ROOT = Path(__file__).resolve().parent


def process(job: Job) -> Path:
    """Fetch, tailor, compile, archive. Returns the application folder."""
    queue.update(job.id, status=Status.TAILORING)
    app_dir = store.create(job)
    store.set_status(app_dir, Status.TAILORING)
    queue.update(job.id, app_dir=str(app_dir))

    posting = fetch.fetch(job.url)

    # The posting is authoritative for company and title; the extension only
    # guessed them from whatever metadata the page happened to expose.
    if posting.company and not job.company:
        queue.update(job.id, company=posting.company)
    store.write(app_dir, "posting.md", posting.to_markdown())

    result = tailor.tailor(posting)

    store.write(app_dir, "resume.tex", result.tex)
    store.write(app_dir, "resume.diff", result.diff or "(no changes)\n")
    store.write(
        app_dir,
        "suggestions.md",
        f"# Tailoring rationale\n\n_Model: {result.model}_\n\n{result.suggestions}\n",
    )

    pdf = texc.compile_pdf(app_dir / "resume.tex", app_dir / "resume.pdf")
    pages = texc.page_count(pdf)
    base_pages = texc.page_count(ROOT / "base" / "resume.pdf") if (
        ROOT / "base" / "resume.pdf"
    ).exists() else None
    note = f"{pages} page(s)"
    if base_pages and pages and pages != base_pages:
        note += f" — master is {base_pages}; check for overflow"

    store.set_status(app_dir, Status.AWAITING_REVIEW, note)
    queue.update(job.id, status=Status.AWAITING_REVIEW)
    return app_dir


def process_safely(job: Job) -> bool:
    try:
        app_dir = process(job)
    except Exception as exc:  # noqa: BLE001 - one bad job must not stop the batch
        detail = f"{type(exc).__name__}: {exc}"
        print(f"  failed: {detail}", file=sys.stderr)
        if isinstance(exc, texc.CompileError) and exc.log:
            print(exc.tail(15), file=sys.stderr)
        queue.update(job.id, status=Status.FAILED, error=detail)
        existing = queue.get(job.id)
        if existing and existing.app_dir:
            app_dir = Path(existing.app_dir)
            store.set_status(app_dir, Status.FAILED, detail)
            if not (app_dir / "error.txt").exists():
                store.write(app_dir, "error.txt", detail + "\n\n" + traceback.format_exc())
        return False
    print(f"  ready for review: {app_dir}")
    return True


def main(argv: list[str]) -> int:
    if len(argv) > 1:
        job = queue.get(argv[1])
        if job is None:
            print(f"unknown job id {argv[1]}", file=sys.stderr)
            return 1
        jobs = [job]
    else:
        jobs = queue.pending()

    if not jobs:
        print("nothing queued")
        return 0

    failures = 0
    for job in jobs:
        print(f"{job.id}  {job.title or job.url}")
        if not process_safely(job):
            failures += 1

    print(f"\n{len(jobs) - failures}/{len(jobs)} ready for review at http://127.0.0.1:8787")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
