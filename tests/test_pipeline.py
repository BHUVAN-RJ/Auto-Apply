"""Pipeline glue, with the network, the model, and lualatex all stubbed out."""

import pytest

import pipeline
from archive import store
from server import queue
from server.models import Job, Status
from tailor.fetch import Posting
from tailor.tailor import TailorResult


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    qpath = tmp_path / "queue.json"
    monkeypatch.setattr(queue, "QUEUE_PATH", qpath)
    monkeypatch.setattr(queue, "LOCK_PATH", qpath.with_suffix(".lock"))
    monkeypatch.setattr(store, "APPLICATIONS", tmp_path / "applications")
    monkeypatch.setattr(store, "INDEX_PATH", tmp_path / "applications" / "index.csv")


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
                     "suggestions.md", "resume.pdf", "status.json"):
        assert (app_dir / artifact).exists(), artifact

    assert store.read_status(app_dir) == Status.AWAITING_REVIEW.value
    assert queue.get(job.id).status == Status.AWAITING_REVIEW
    assert queue.get(job.id).app_dir == str(app_dir)


def test_pipeline_never_advances_past_review(monkeypatch):
    """The agent-side pipeline must not reach a submitted or filled state."""
    stub_success(monkeypatch)
    job, _ = queue.add(Job(url="https://example.com/jobs/1"))
    pipeline.process(job)
    assert queue.get(job.id).status not in (Status.FILLED, Status.SUBMITTED, Status.APPROVED)


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


def test_a_mismatch_is_skipped_not_failed(monkeypatch):
    """A poor-fit posting stops cleanly and records why, without tailoring."""
    stub_success(monkeypatch)

    def mismatch(posting, **kwargs):
        raise pipeline.tailor.Mismatch("MISMATCH: requires a security clearance.")

    monkeypatch.setattr(pipeline.tailor, "tailor", mismatch)
    job, _ = queue.add(Job(url="https://example.com/jobs/1"))

    app_dir = pipeline.process(job)

    assert queue.get(job.id).status == Status.SKIPPED
    assert "clearance" in (app_dir / "mismatch.md").read_text()
    assert not (app_dir / "resume.tex").exists(), "nothing may be tailored on a mismatch"


def test_main_with_no_queue_is_a_clean_exit(capsys):
    assert pipeline.main(["pipeline.py"]) == 0
    assert "nothing queued" in capsys.readouterr().out
