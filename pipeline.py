"""Phase 2 pipeline: queued job -> tailored, compiled, archived, awaiting review.

This is the half that runs before checkpoint 1. It deliberately stops at
AWAITING_REVIEW; the browser fill loop is a separate phase and only ever runs
against a job a human has approved.

    python pipeline.py            # process every queued job
    python pipeline.py <job_id>   # process one
"""

from __future__ import annotations

import json
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Optional

from archive import store
from server import queue
from server import screen as screen_server
from server.models import Job, Status
from tailor import cover, fetch, profile, tailor
from tex import compile as texc

ROOT = Path(__file__).resolve().parent


def base_page_count() -> int:
    """Page count of the master resume, which the tailored one must match.

    Falls back to 1, since a one-page resume is the assumption this project is
    built around.
    """
    base_pdf = ROOT / "base" / "resume.pdf"
    if base_pdf.exists():
        return texc.page_count(base_pdf) or 1
    base_tex = ROOT / "base" / "resume.tex"
    if not base_tex.exists():
        return 1
    with tempfile.TemporaryDirectory() as tmp:
        pdf = texc.compile_pdf(base_tex, Path(tmp) / "base.pdf")
        return texc.page_count(pdf) or 1


def make_page_check(base_tex: Path):
    """Compile a candidate resume and report its page count.

    Sibling files of the master resume travel with it, so a template split
    across a .cls or .sty still compiles. A candidate that fails to compile
    returns None, which the tailor treats as "unknown" and accepts; the real
    compile later in the pipeline will surface the error properly.
    """
    def check(candidate_tex: str):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            src = work / "candidate.tex"
            src.write_text(candidate_tex)
            for sibling in base_tex.parent.iterdir():
                if sibling.is_file() and sibling.suffix in (".cls", ".sty"):
                    (work / sibling.name).write_bytes(sibling.read_bytes())
            try:
                pdf = texc.compile_pdf(src, work / "candidate.pdf")
            except texc.CompileError:
                return None
            return texc.page_count(pdf)

    return check


def retailor(job: Job, instruction: str) -> Path:
    """Run the job again with a reviewer instruction added to the prompt.

    Produces a new application folder rather than editing the existing one,
    so the rejected version stays on disk next to the reason it was rejected.
    """
    return process(job, extra_instruction=instruction)


def allocate(job: Job) -> Path:
    """A fresh application folder, recorded on the queue row."""
    app_dir = store.create(job)
    store.set_status(app_dir, Status.TAILORING)
    queue.update(job.id, app_dir=str(app_dir))
    return app_dir


def process(job: Job, extra_instruction: str = "") -> Path:
    """Fetch, tailor, compile, archive. Returns the application folder."""
    # A fresh run clears the last one's error; otherwise a retry that
    # succeeds still shows "Failed" over a perfectly good resume.
    queue.update(job.id, status=Status.TAILORING, error=None)

    # Fetch before the folder is allocated: its name carries the company, and
    # the extension often captures none (a Jobright link that lands on Ashby
    # exposes no metadata), so every first run was `unknown-company_...`.
    try:
        posting = fetch.fetch(job.url)
    except Exception:
        # A failed fetch still gets its own folder, so the error lands next
        # to this run's record and not in the previous run's.
        allocate(job)
        raise

    # The posting is authoritative for company and title; the extension only
    # guessed them from whatever metadata the page happened to expose.
    if posting.company and not job.company:
        job = queue.update(job.id, company=posting.company) or job

    app_dir = allocate(job)
    store.write(app_dir, "posting.md", posting.to_markdown())
    write_screen(app_dir, job, posting)

    base_tex = ROOT / "base" / "resume.tex"
    target = base_page_count()
    stories = pick_stories(app_dir, posting)
    try:
        result = tailor.tailor(
            posting,
            page_check=make_page_check(base_tex),
            target_pages=target,
            extra_instruction=extra_instruction,
            profile=tailor.load_profile(stories=stories),
        )
    except tailor.Mismatch as exc:
        # A poor-fit verdict is advice, not a decision. The job still stops at
        # checkpoint 1 with the reasoning on show; only the human closes it.
        store.write(
            app_dir,
            "mismatch.md",
            f"# The model thinks this is a poor fit\n\n{exc}\n\n"
            "Nothing was tailored. Reject it if you agree, or re-tailor with a "
            "note if you think the model is wrong.\n",
        )
        store.set_status(app_dir, Status.AWAITING_REVIEW, f"poor fit: {exc}")
        queue.update(job.id, status=Status.AWAITING_REVIEW)
        print(f"  flagged as a poor fit, waiting on you: {exc}")
        return app_dir

    store.write(app_dir, "resume.tex", result.tex)
    store.write(app_dir, "resume.diff", result.diff or "(no changes)\n")
    store.write(
        app_dir,
        "suggestions.md",
        f"# Tailoring rationale\n\n_Model: {result.model} "
        f"(attempt {result.attempts})_\n\n"
        + (f"**Fit:** {result.verdict}\n\n" if result.verdict else "")
        + (
            "**Length warnings:** " + "; ".join(result.warnings) + "\n\n"
            if result.warnings
            else ""
        )
        + f"{result.suggestions}\n",
    )

    pdf = texc.compile_pdf(app_dir / "resume.tex", app_dir / "resume.pdf")
    pages = texc.page_count(pdf)
    note = f"{pages} page(s)"
    if pages is not None and pages != target:
        note += f" — master is {target}; the page-count gate did not hold"

    note += "; " + write_cover_letter(app_dir, posting, result.tex, extra_instruction, stories)

    store.set_status(app_dir, Status.AWAITING_REVIEW, note)
    queue.update(job.id, status=Status.AWAITING_REVIEW)
    return app_dir


def write_screen(app_dir: Path, job: Job, posting) -> None:
    """Archive the auto-reject screen next to the posting.

    The extension usually screened this URL when the page was opened, so this
    is a cache hit; otherwise the model runs once here. Not fatal: a screen
    that failed is a line on the review page, not a reason to skip tailoring.
    """
    try:
        result, _ = screen_server.screen_url(job.url, posting.text, title=posting.title)
        store.write(app_dir, "screen.json", json.dumps(result.to_dict(), indent=2) + "\n")
        if result.flags:
            print(f"  screen: {result.verdict}, {len(result.flags)} flag(s)")
    except Exception as exc:  # noqa: BLE001 - see docstring
        store.write(app_dir, "screen_error.txt", f"{type(exc).__name__}: {exc}\n")
        print(f"  screen failed: {exc}", file=sys.stderr)


def pick_stories(app_dir: Path, posting) -> list[str]:
    """Which experience documents this posting gets, recorded in the folder
    so the cover letter and the form answers reuse the same pick and the
    review page can show it. A failed pick is no stories, not a failed job."""
    try:
        stories = profile.pick(posting.text)
    except Exception as exc:  # noqa: BLE001 - advice, not a step
        print(f"  story pick failed: {exc}", file=sys.stderr)
        stories = []
    store.write(app_dir, "stories_used.txt", "\n".join(stories) + ("\n" if stories else ""))
    if stories:
        print(f"  stories: {', '.join(stories)}")
    return stories


def write_cover_letter(app_dir: Path, posting, resume_tex: str, extra_instruction: str = "",
                       stories: Optional[list[str]] = None) -> str:
    """Write the letter next to the resume. Returns a one-line note for the status.

    Deliberately not fatal: the resume it sits beside cost several model
    round-trips and a compile, and a letter that failed to come out is a
    reason to look at cover_letter_error.txt, not to throw that away. The
    review page shows the letter when it exists and the error when it does
    not.
    """
    try:
        letter = cover.write(posting, resume_tex, tailor.load_profile(stories=stories), extra_instruction)
        store.write(app_dir, "cover_letter.md", letter.text + "\n")
        store.write(app_dir, "cover_letter.tex", cover.to_tex(letter.text))
        texc.compile_pdf(app_dir / "cover_letter.tex", app_dir / "cover_letter.pdf")
    except Exception as exc:  # noqa: BLE001 - see docstring
        detail = f"{type(exc).__name__}: {exc}"
        if isinstance(exc, texc.CompileError) and exc.log:
            detail += "\n\n" + exc.tail(15)
        store.write(app_dir, "cover_letter_error.txt", detail + "\n")
        print(f"  cover letter failed: {detail.splitlines()[0]}", file=sys.stderr)
        return "no cover letter (see cover_letter_error.txt)"
    return f"cover letter {letter.words} words"


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
            # Keep what the model actually produced. A rejection that only
            # appears on a later retry cannot be diagnosed from its reason.
            for record in getattr(exc, "attempts", []) or []:
                name = f"rejected_attempt_{record['attempt']}.tex"
                if not (app_dir / name).exists():
                    store.write(
                        app_dir, name,
                        f"% rejected: {record['reason']}\n{record['tex']}",
                    )
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
