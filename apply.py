"""Checkpoint 2: fill approved applications, then stop.

Separate from pipeline.py on purpose. That one runs unattended over the queue;
this one drives a visible browser on your machine and only ever touches jobs
you have already approved at checkpoint 1.

    python apply.py            # fill every approved job
    python apply.py <job_id>   # fill one
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

from archive import store
from browser import fill as filler
from tailor import answers, profile, tailor
from server import queue
from server.models import Job, Status

ROOT = Path(__file__).resolve().parent
# browser-use happens to load .env itself, but only once imported, which is
# after the settings below are read. Load it here so nothing depends on that.
load_dotenv(ROOT / ".env")

# The file name the recruiter sees. The archive keeps `resume.pdf` as its
# artifact; the upload is a copy under this name. Personal, so it comes from
# .env rather than the public repo.
RESUME_FILENAME = "AUTOPILOT_RESUME_FILENAME"
COVER_LETTER_FILENAME = "AUTOPILOT_COVER_LETTER_FILENAME"
DEFAULT_RESUME_FILENAME = "Resume"
DEFAULT_COVER_LETTER_FILENAME = "Cover_Letter"


def _filename(env: str, default: str) -> str:
    """An upload's stem, with spaces as underscores and no path characters."""
    name = os.environ.get(env, "").strip() or default
    name = name.removesuffix(".pdf")
    name = "_".join(name.split())
    return "".join(c for c in name if c.isalnum() or c in "_-") or default


def resume_filename() -> str:
    return _filename(RESUME_FILENAME, DEFAULT_RESUME_FILENAME)


def cover_letter_filename() -> str:
    return _filename(COVER_LETTER_FILENAME, DEFAULT_COVER_LETTER_FILENAME)


def upload_copy(app_dir: Path, source: str = "resume.pdf", stem: Optional[str] = None) -> Optional[Path]:
    """An artifact under the name it should be uploaded as.

    Made once per application folder and kept, so the archive records exactly
    what went on the form. None when the source does not exist, which for
    the cover letter is an ordinary outcome.
    """
    if not (app_dir / source).exists():
        return None
    target = app_dir / f"{stem or resume_filename()}.pdf"
    if not target.exists():
        shutil.copyfile(app_dir / source, target)
    return target


def condensed(errors: list[str], limit: int = 5, width: int = 160) -> list[str]:
    """The distinct errors, each cut to one line, repeats counted not repeated.

    browser-use repeats the same error on every retry, in full, with a code
    sample. Five copies of a 400-character message told the reviewer one
    thing five times.
    """
    counts: dict[str, int] = {}
    for error in errors:
        line = " ".join(str(error).split())
        line = line.split(" To fix:")[0].split(" Example:")[0]
        if len(line) > width:
            line = line[: width - 1] + "…"
        counts[line] = counts.get(line, 0) + 1
    out = [f"{line} (x{n})" if n > 1 else line for line, n in counts.items()]
    if len(out) > limit:
        out = out[:limit] + [f"… and {len(out) - limit} more, see the log"]
    return out


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

    cover_letter = upload_copy(app_dir, "cover_letter.pdf", cover_letter_filename())
    # Open questions on the form are answered by the tailoring model from
    # this application's own material, never by the browser model.
    answerer = answers.Answerer(answers.Context.from_app_dir(
        app_dir, profile=tailor.load_profile(stories=profile.read_used(app_dir)),
        applicant=filler.applicant_details()))
    try:
        result = filler.fill(
            job.url, upload_copy(app_dir), app_dir / "fill_screenshot.png",
            cover_letter_pdf=cover_letter, answerer=answerer,
        )
    except Exception as exc:  # noqa: BLE001 - one bad form must not stop the batch
        detail = f"{type(exc).__name__}: {exc}"
        print(f"  failed: {detail}", file=sys.stderr)
        queue.update(job.id, status=Status.FAILED, error=detail)
        store.set_status(app_dir, Status.FAILED, detail)
        return False

    if answerer.answers:
        store.write_or_append(app_dir, "answers.md", answerer.to_markdown())

    note = result.summary()
    if answerer.answers:
        note += f"; {len(answerer.answers)} question(s) answered"
    body = [f"# Fill notes\n\n_{note}_\n\n{result.notes}\n"]
    if result.errors:
        body.append("\n## Errors\n\n" + "\n".join(f"- {e}" for e in condensed(result.errors)) + "\n")
    body.append(f"\nFull log: `data/apply_{job.id}.log`\n")
    store.write_or_append(app_dir, "fill_notes.md", "".join(body))

    if not result.ok:
        # The browser ran but the form was not filled. Saying "filled" here
        # would send the reviewer to check a screenshot of nothing.
        detail = result.errors[0] if result.errors else "the agent never finished"
        detail = f"fill did not complete: {detail[:300]}"
        store.set_status(app_dir, Status.FAILED, detail)
        queue.update(job.id, status=Status.FAILED, error=detail)
        print(f"  did not fill: {detail}", file=sys.stderr)
        print(f"  see data/apply_{job.id}.log", file=sys.stderr)
        return False

    # FILLED is terminal for the agent. Only a human moves it to SUBMITTED.
    store.set_status(app_dir, Status.FILLED, note)
    queue.update(job.id, status=Status.FILLED)
    print(f"  filled, {note}")
    print("  the browser window is still open on the form — check it, submit it")
    print("  yourself, then mark it submitted at http://127.0.0.1:8787")
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
