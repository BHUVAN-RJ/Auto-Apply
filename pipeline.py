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

import paths
from archive import store
from browser import autofill, chrome
from server import postings, queue, runner, seen, settings
from server import screen as screen_server
from server.models import Job, Status
from tailor import cover, fetch, profile, quality, tailor
from tex import compile as texc
from tex import mark

ROOT = Path(__file__).resolve().parent


def base_page_count() -> int:
    """Page count of the master resume, which the tailored one must match.

    Falls back to 1, since a one-page resume is the assumption this project is
    built around.
    """
    base_pdf = paths.BASE / "resume.pdf"
    if base_pdf.exists():
        return texc.page_count(base_pdf) or 1
    base_tex = paths.BASE / "resume.tex"
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


def retailor(job: Job, instruction: str, keep_status: bool = False) -> Path:
    """Run the job again with a reviewer instruction added to the prompt.

    Produces a new application folder rather than editing the existing one,
    so the rejected version stays on disk next to the reason it was rejected.

    `keep_status` is the re-tailor of a job that is already filled or
    submitted: the documents are written and the row points at them, but
    the job does not walk back to checkpoint 1 and no fill is started.
    The new resume is there to be dragged onto a form by hand.
    """
    return process(job, extra_instruction=instruction, keep_status=keep_status)


def fetch_posting(job: Job) -> fetch.Posting:
    """The posting text: the best of what the server fetched, what the
    browser saw on the employer's page, and Jobright's copy.

    An Apply button can land on a bare application form, and an ATS page can
    render client-side, so the fetch may come back with a page that clears
    the length gate on cookie notices and field labels alone. Each copy is
    scored on what a description is made of (`tailor.quality`, no model);
    the fetched page wins ties, being the posting itself, and loses when
    it has clearly less than the best copy. Which one won is written into
    the posting's header.
    """
    candidates: list[tuple[str, fetch.Posting]] = []
    fetched: fetch.Posting | None = None
    error: Exception | None = None
    try:
        fetched = fetch.fetch(job.url)
        candidates.append(("employer page", fetched))
    except Exception as exc:  # noqa: BLE001 - the fallbacks decide
        error = exc
    for saved in postings.fallbacks(job.id, job.url):
        name = "Jobright's copy" if seen.is_source_page(saved.url) else "the page the browser saw"
        candidates.append((name, fetch.Posting(url=job.url, text=saved.text, title=saved.title,
                                               company=saved.company)))
    if not candidates:
        raise error or RuntimeError(f"no posting text for {job.url}")

    scores = [(quality.score(p.text), i) for i, (_, p) in enumerate(candidates)]
    top_score, top = max(scores, key=lambda item: (item[0], -item[1]))
    if top_score == 0 and fetched is None:
        raise error or RuntimeError(f"no posting text for {job.url}")
    if fetched is not None and (top_score == 0 or scores[0][0] >= top_score * quality.GOOD_ENOUGH):
        top = 0
    name, posting = candidates[top]
    if top != 0 or error is not None:
        why = f"fetch failed ({error})" if error else f"fetched page scored {scores[0][0]} against {top_score}"
        print(f"{why}; using {name} ({candidates[top][1].url})")
    posting.text_source = name
    # The fetched page may have no company; Jobright's copy usually does.
    if not posting.company:
        posting.company = next((p.company for _, p in candidates if p.company), "")
    if not posting.title:
        posting.title = next((p.title for _, p in candidates if p.title), "")
    return posting


def allocate(job: Job) -> Path:
    """A fresh application folder, recorded on the queue row."""
    app_dir = store.create(job)
    store.set_status(app_dir, Status.TAILORING)
    queue.update(job.id, app_dir=str(app_dir))
    return app_dir


def process(job: Job, extra_instruction: str = "", keep_status: bool = False) -> Path:
    """Fetch, tailor, compile, archive. Returns the application folder.

    `keep_status` leaves the queue row's status alone (see `retailor`): the
    folder is written and the row points at it, but a filled or submitted
    job stays filled or submitted and nothing is approved or launched.
    """
    # A fresh run clears the last one's error; otherwise a retry that
    # succeeds still shows "Failed" over a perfectly good resume.
    held = (queue.get(job.id) or job).status if keep_status else None
    if keep_status:
        queue.update(job.id, error=None)
    else:
        queue.update(job.id, status=Status.TAILORING, error=None)

    # Fetch before the folder is allocated: its name carries the company, and
    # the extension often captures none (a Jobright link that lands on Ashby
    # exposes no metadata), so every first run was `unknown-company_...`.
    try:
        posting = fetch_posting(job)
    except Exception:
        # A failed fetch still gets its own folder, so the error lands next
        # to this run's record and not in the previous run's.
        allocate(job)
        raise

    # The posting is authoritative for company and title; the extension only
    # guessed them from whatever metadata the page happened to expose, which
    # on Jobright is "Job Recommendations | Jobright AI". Jobright's own copy
    # of the posting names the role and employer best; the fetched page next.
    src_title, src_company = postings.source_meta(job.url)
    title = src_title or posting.title or job.title
    company = job.company or src_company or posting.company
    if (title, company) != (job.title, job.company):
        job = queue.update(job.id, title=title, company=company) or job

    app_dir = allocate(job)
    store.write(app_dir, "posting.md", posting.to_markdown())
    write_screen(app_dir, job, posting)

    base_tex = paths.BASE / "resume.tex"
    target = base_page_count()
    stories = pick_stories(app_dir, posting)
    try:
        result = tailor.tailor(
            posting,
            page_check=make_page_check(base_tex),
            target_pages=target,
            extra_instruction=extra_instruction,
            profile=tailor.load_profile(stories=stories),
            model=job.tailor_model,
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
        store.set_status(app_dir, held or Status.AWAITING_REVIEW, f"poor fit: {exc}")
        if not keep_status:
            queue.update(job.id, status=Status.AWAITING_REVIEW)
        print(f"  flagged as a poor fit, waiting on you: {exc}")
        tell_tab(job, "done", "Poor fit, says the model; decide on the review page")
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
        # What the checker sent back, so a resume that came back barely
        # changed can be read as "the model kept hitting a rule" rather
        # than "the model had nothing to say".
        + (
            "**Attempts rejected before this one:**\n"
            + "\n".join(f"- {line}" for line in result.rejections) + "\n\n"
            if result.rejections
            else ""
        )
        + f"{result.suggestions}\n",
    )

    pdf = texc.compile_pdf(app_dir / "resume.tex", app_dir / "resume.pdf")
    pages = texc.page_count(pdf)
    write_marked(app_dir, base_tex.read_text(), result.tex)
    note = f"{pages} page(s)"
    if pages is not None and pages != target:
        note += f" — master is {target}; the page-count gate did not hold"

    note += "; " + write_cover_letter(app_dir, posting, result.tex, extra_instruction, stories,
                                      model=job.tailor_model)

    store.set_status(app_dir, held or Status.AWAITING_REVIEW, note)
    if keep_status:
        # Documents only: the job keeps the standing it had, and the fill
        # is not touched. The new resume is for the human to hand over.
        print(f"  re-tailored, status left at {held.value if held else 'unchanged'}: {app_dir}")
        return app_dir
    queue.update(job.id, status=Status.AWAITING_REVIEW)
    if not auto_approve(app_dir, job):
        tell_tab(job, "done", "Tailored; approve it on the review page")
    return app_dir


def auto_approve(app_dir: Path, job: Job) -> bool:
    """Checkpoint 1 without a click, when the job's auto-approve box (the
    banner, at capture) or failing that the `auto_fill` switch says so, and
    the screen did not reject. The clean verdict is what queued the job in
    the first place, so the fill starts as soon as the documents exist and
    the human decides once, on the filled form. Never for a poor-fit verdict
    (that returns before this) or a `reject` screen. The fill itself still
    stops at checkpoint 2; nothing submits."""
    wanted = settings.auto_fill() if job.auto_fill is None else bool(job.auto_fill)
    if not wanted:
        return False
    try:
        verdict = json.loads((app_dir / "screen.json").read_text()).get("verdict")
    except (OSError, ValueError):
        verdict = None
    if verdict == "reject":
        tell_tab(job, "done", "The screen says reject; decide on the review page")
        return False
    store.set_status(app_dir, Status.APPROVED, "approved by autopilot: clean screen, tailored")
    queue.update(job.id, status=Status.APPROVED)
    pid = runner.start_fill(job.id)
    if pid:
        store.set_status(app_dir, Status.APPROVED, f"filling started (pid {pid})")
        tell_tab(job, "working", "Tailored; the browser agent is taking over")
        print(f"  approved by autopilot, fill started (pid {pid})")
    else:
        tell_tab(job, "done", "Tailored; start the fill on the review page")
        print("  approved by autopilot; the fill did not start (autofill off or already running)")
    return True


def tell_tab(job: Job, state: str, note: str) -> None:
    """Set the banner on the job's open tab, so the human sees the pipeline
    working between Jobright's autofill and the browser agent. Advice only:
    no tab, no browser, no harm."""
    try:
        autofill.notify_url(chrome.cdp_url(chrome.port()), job.url, state, note)
    except Exception as error:  # noqa: BLE001 - the indicator is advice
        print(f"  banner: {error}", file=sys.stderr)


def write_marked(app_dir: Path, original: str, tailored: str) -> None:
    """The tailored resume with its changes coloured, for the review page's
    "changes" and "before and after" views. Derived from the source that
    already compiled, so a failure here is a missing view, not a failed run."""
    for mode in mark.MODES:
        name = f"resume_{mode}"
        try:
            store.write(app_dir, f"{name}.tex", mark.mark(original, tailored, mode))
            texc.compile_pdf(app_dir / f"{name}.tex", app_dir / f"{name}.pdf")
        except Exception as error:  # noqa: BLE001 - a view, never the run
            print(f"{name}: {error}")


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
                       stories: Optional[list[str]] = None, model: Optional[str] = None) -> str:
    """Write the letter next to the resume. Returns a one-line note for the status.

    Deliberately not fatal: the resume it sits beside cost several model
    round-trips and a compile, and a letter that failed to come out is a
    reason to look at cover_letter_error.txt, not to throw that away. The
    review page shows the letter when it exists and the error when it does
    not.
    """
    try:
        letter = cover.write(posting, resume_tex, tailor.load_profile(stories=stories),
                             extra_instruction, model=model)
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
        tell_tab(job, "error", f"Tailoring failed: {detail.splitlines()[0][:80]}")
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
