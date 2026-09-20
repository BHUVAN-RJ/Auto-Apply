"""Pipeline glue, with the network, the model, and lualatex all stubbed out."""

import json

import pytest

import pipeline
from archive import store
from server import queue, settings
from server.models import Job, Status
from tailor.fetch import Posting
from tailor.cover import CoverLetter
from tailor.screen import Flag, Screen
from tailor.tailor import TailorResult

LETTER = "Dear Hiring Manager,\n\nOne paragraph.\n\nSincerely,\nJane Doe"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    # Never the network: the letter step calls the model unless stubbed.
    monkeypatch.setattr(pipeline.cover, "write",
                        lambda posting, tex, profile="", extra="": CoverLetter(LETTER, "test/model"))
    # Same for the screen: it reads base/applicant.md and calls the model.
    monkeypatch.setattr(pipeline.screen_server, "screen_url",
                        lambda url, text, title="", force=False: (Screen(verdict="ok"), True))
    qpath = tmp_path / "queue.json"
    monkeypatch.setattr(queue, "QUEUE_PATH", qpath)
    monkeypatch.setattr(queue, "LOCK_PATH", qpath.with_suffix(".lock"))
    monkeypatch.setattr(store, "APPLICATIONS", tmp_path / "applications")
    monkeypatch.setattr(store, "INDEX_PATH", tmp_path / "applications" / "index.csv")
    # Checkpoint 1 waits by default here; the auto_fill tests switch it on.
    monkeypatch.setattr(settings, "SETTINGS_PATH", tmp_path / "settings.json")
    settings.save(auto_fill=False)
    # Never a real apply.py, never a real browser.
    monkeypatch.setattr(pipeline.runner, "start_fill", lambda job_id, log_dir=None: None)
    monkeypatch.setattr(pipeline, "tell_tab", lambda job, state, note: None)


def stub_success(monkeypatch):
    monkeypatch.setattr(pipeline.fetch, "fetch", lambda url: Posting(
        url=url, text="Distributed systems role. " * 20,
        title="Backend Engineer", company="Example Corp"))
    monkeypatch.setattr(pipeline, "base_page_count", lambda: 1)
    monkeypatch.setattr(pipeline.tailor, "tailor", lambda posting, **kwargs: TailorResult(
        tex="\\documentclass{article}\\begin{document}x\\end{document}",
        suggestions="- reordered experience",
        diff="--- a/resume.tex\n+++ b/resume.tex\n",
        model="test/model"))
    monkeypatch.setattr(pipeline.texc, "compile_pdf",
                        lambda src, out, **k: (out.write_bytes(b"%PDF-1.5 fake"), out)[1])
    monkeypatch.setattr(pipeline.texc, "page_count", lambda p: 1)


def test_happy_path_stops_at_awaiting_review(monkeypatch):
    stub_success(monkeypatch)
    job, _ = queue.add(Job(url="https://example.com/jobs/1", title="Backend Engineer"))

    app_dir = pipeline.process(job)

    for artifact in ("job.json", "posting.md", "resume.tex", "resume.diff",
                     "suggestions.md", "resume.pdf", "status.json",
                     "cover_letter.md", "cover_letter.tex", "cover_letter.pdf"):
        assert (app_dir / artifact).exists(), artifact
    assert (app_dir / "cover_letter.md").read_text().startswith("Dear Hiring Manager")
    assert "Sincerely, \\\\\nJane Doe" in (app_dir / "cover_letter.tex").read_text()


def test_screen_verdict_is_archived_and_changes_nothing(monkeypatch):
    stub_success(monkeypatch)
    flagged = Screen(verdict="reject", flags=[Flag("visa", "hard", "no sponsorship", "needs it")])
    monkeypatch.setattr(pipeline.screen_server, "screen_url",
                        lambda url, text, title="", force=False: (flagged, False))
    job, _ = queue.add(Job(url="https://example.com/jobs/1"))

    app_dir = pipeline.process(job)

    saved = json.loads((app_dir / "screen.json").read_text())
    assert saved["verdict"] == "reject" and saved["flags"][0]["category"] == "visa"
    # A reject screen is advice: the job still tailors and still stops at review.
    assert (app_dir / "resume.pdf").exists()
    assert queue.get(job.id).status == Status.AWAITING_REVIEW


def test_a_failed_screen_does_not_cost_the_resume(monkeypatch):
    stub_success(monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("model down")

    monkeypatch.setattr(pipeline.screen_server, "screen_url", boom)
    job, _ = queue.add(Job(url="https://example.com/jobs/1"))

    app_dir = pipeline.process(job)

    assert "model down" in (app_dir / "screen_error.txt").read_text()
    assert (app_dir / "resume.pdf").exists()
    assert queue.get(job.id).status == Status.AWAITING_REVIEW


def test_a_failed_cover_letter_does_not_cost_the_resume(monkeypatch):
    """The letter is the cheap part. Its failure is recorded, not propagated."""
    stub_success(monkeypatch)

    def boom(*args, **kwargs):
        raise RuntimeError("model returned nothing")

    monkeypatch.setattr(pipeline.cover, "write", boom)
    job, _ = queue.add(Job(url="https://example.com/jobs/1"))
    app_dir = pipeline.process(job)

    assert (app_dir / "resume.pdf").exists()
    assert not (app_dir / "cover_letter.pdf").exists()
    assert "model returned nothing" in (app_dir / "cover_letter_error.txt").read_text()
    assert queue.get(job.id).status == Status.AWAITING_REVIEW

    assert store.read_status(app_dir) == Status.AWAITING_REVIEW.value
    assert queue.get(job.id).status == Status.AWAITING_REVIEW
    assert queue.get(job.id).app_dir == str(app_dir)


def test_pipeline_never_advances_past_review(monkeypatch):
    """The agent-side pipeline must not reach a submitted or filled state."""
    stub_success(monkeypatch)
    job, _ = queue.add(Job(url="https://example.com/jobs/1"))
    pipeline.process(job)
    assert queue.get(job.id).status not in (Status.FILLED, Status.SUBMITTED, Status.APPROVED)


def test_auto_fill_approves_a_clean_job_and_starts_the_fill(monkeypatch):
    """With the switch on, checkpoint 1 is the clean verdict that queued the
    job: the fill starts as soon as the documents exist. Still never past
    APPROVED here; apply.py owns FILLING and stops at checkpoint 2."""
    stub_success(monkeypatch)
    settings.save(auto_fill=True)
    started, told = [], []
    monkeypatch.setattr(pipeline.runner, "start_fill", lambda job_id, log_dir=None: started.append(job_id) or 4242)
    monkeypatch.setattr(pipeline, "tell_tab", lambda job, state, note: told.append(state))
    job, _ = queue.add(Job(url="https://example.com/jobs/1"))
    app_dir = pipeline.process(job)
    assert started == [job.id]
    assert queue.get(job.id).status == Status.APPROVED
    assert store.read_status(app_dir) == Status.APPROVED.value
    assert told[-1] == "working"


def test_auto_fill_leaves_a_rejected_screen_at_checkpoint_one(monkeypatch):
    stub_success(monkeypatch)
    settings.save(auto_fill=True)
    monkeypatch.setattr(pipeline.screen_server, "screen_url",
                        lambda url, text, title="", force=False: (Screen(verdict="reject", flags=[
                            Flag(category="clearance", severity="hard", quote="TS/SCI required", reason="clearance")]), True))
    started = []
    monkeypatch.setattr(pipeline.runner, "start_fill", lambda job_id, log_dir=None: started.append(job_id) or 1)
    job, _ = queue.add(Job(url="https://example.com/jobs/1"))
    pipeline.process(job)
    assert not started and queue.get(job.id).status == Status.AWAITING_REVIEW


def test_auto_fill_leaves_a_poor_fit_at_checkpoint_one(monkeypatch):
    stub_success(monkeypatch)
    settings.save(auto_fill=True)
    monkeypatch.setattr(pipeline.tailor, "tailor",
                        lambda posting, **kwargs: (_ for _ in ()).throw(pipeline.tailor.Mismatch("wrong field")))
    started = []
    monkeypatch.setattr(pipeline.runner, "start_fill", lambda job_id, log_dir=None: started.append(job_id) or 1)
    job, _ = queue.add(Job(url="https://example.com/jobs/1"))
    pipeline.process(job)
    assert not started and queue.get(job.id).status == Status.AWAITING_REVIEW


def test_fetch_failure_marks_the_job_failed(monkeypatch):
    stub_success(monkeypatch)

    def boom(url):
        raise pipeline.fetch.JavaScriptRequired("renders client-side")

    monkeypatch.setattr(pipeline.fetch, "fetch", boom)
    job, _ = queue.add(Job(url="https://example.com/jobs/1"))

    assert pipeline.process_safely(job) is False
    stored = queue.get(job.id)
    assert stored.status == Status.FAILED
    assert "renders client-side" in stored.error
    assert (pipeline.Path(stored.app_dir) / "error.txt").exists()


def test_one_failure_does_not_stop_the_batch(monkeypatch, capsys):
    stub_success(monkeypatch)
    calls = {"n": 0}

    def flaky(url):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("first job explodes")
        return Posting(url=url, text="x" * 500, title="Second", company="Example Corp")

    monkeypatch.setattr(pipeline.fetch, "fetch", flaky)
    queue.add(Job(url="https://example.com/jobs/1"))
    queue.add(Job(url="https://example.com/jobs/2"))

    assert pipeline.main(["pipeline.py"]) == 1
    statuses = {j.url: j.status for j in queue.all_jobs()}
    assert statuses["https://example.com/jobs/1"] == Status.FAILED
    assert statuses["https://example.com/jobs/2"] == Status.AWAITING_REVIEW


def test_a_mismatch_waits_for_the_human_rather_than_closing_itself(monkeypatch):
    """The agent may recommend dropping a job; only the human decides."""
    stub_success(monkeypatch)

    def mismatch(posting, **kwargs):
        raise pipeline.tailor.Mismatch("MISMATCH: requires a security clearance.")

    monkeypatch.setattr(pipeline.tailor, "tailor", mismatch)
    job, _ = queue.add(Job(url="https://example.com/jobs/1"))

    app_dir = pipeline.process(job)

    assert queue.get(job.id).status == Status.AWAITING_REVIEW, (
        "a poor-fit verdict is advice; the agent must not close the job itself")
    assert queue.get(job.id).status != Status.SKIPPED
    assert "clearance" in (app_dir / "mismatch.md").read_text()
    assert not (app_dir / "resume.tex").exists(), "nothing may be tailored on a mismatch"


def test_main_with_no_queue_is_a_clean_exit(capsys):
    assert pipeline.main(["pipeline.py"]) == 0
    assert "nothing queued" in capsys.readouterr().out


def test_a_successful_rerun_clears_the_previous_failure(monkeypatch):
    stub_success(monkeypatch)
    job, _ = queue.add(Job(url="https://example.com/jobs/1"))
    queue.update(job.id, status=Status.FAILED, error="TailorError: gave up")
    pipeline.process(queue.get(job.id))
    after = queue.get(job.id)
    assert after.status == Status.AWAITING_REVIEW and after.error is None


def test_the_folder_is_named_after_the_posting_company(monkeypatch):
    """The extension often captures no company; the posting always has one.

    The folder used to be allocated before the fetch, so every first run of
    a Jobright link was `unknown-company_...`.
    """
    stub_success(monkeypatch)
    monkeypatch.setattr(pipeline.fetch, "fetch", lambda url: pipeline.fetch.Posting(
        url=url, text="We need a backend engineer.", title="Backend Engineer",
        company="Cherry Technologies, Inc."))
    job, _ = queue.add(Job(url="https://example.com/jobs/1", title="Backend Engineer"))

    app_dir = pipeline.process(job)

    assert "_cherry-technologies-inc_" in app_dir.name
    assert "unknown-company" not in app_dir.name
    assert queue.get(job.id).company == "Cherry Technologies, Inc."


def test_the_jobs_own_auto_approve_answer_beats_the_switch(monkeypatch):
    stub_success(monkeypatch)
    settings.save(auto_fill=True)
    started = []
    monkeypatch.setattr(pipeline.runner, "start_fill", lambda job_id, log_dir=None: started.append(job_id) or 1)
    job, _ = queue.add(Job(url="https://example.com/jobs/1", auto_fill=False))
    pipeline.process(job)
    assert not started and queue.get(job.id).status == Status.AWAITING_REVIEW
    settings.save(auto_fill=False)
    job, _ = queue.add(Job(url="https://example.com/jobs/2", auto_fill=True))
    pipeline.process(job)
    assert started == [job.id] and queue.get(job.id).status == Status.APPROVED
