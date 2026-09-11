"""Checkpoint 2 orchestration, with the browser stubbed out."""

import pytest

import apply
from archive import store
from browser.fill import FillResult
from server import queue
from server.models import Job, Status


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    qpath = tmp_path / "queue.json"
    monkeypatch.setattr(queue, "QUEUE_PATH", qpath)
    monkeypatch.setattr(queue, "LOCK_PATH", qpath.with_suffix(".lock"))
    monkeypatch.setattr(store, "APPLICATIONS", tmp_path / "applications")
    monkeypatch.setattr(store, "INDEX_PATH", tmp_path / "applications" / "index.csv")


def approved_job(status=Status.APPROVED):
    job, _ = queue.add(Job(url="https://example.com/jobs/1", title="Backend Engineer"))
    app_dir = store.create(job)
    store.write(app_dir, "resume.pdf", b"%PDF-1.5 fake")
    store.set_status(app_dir, status)
    queue.update(job.id, status=status, app_dir=str(app_dir))
    return queue.get(job.id), app_dir


def stub_fill(monkeypatch, **overrides):
    calls = {}

    def fake(url, resume, screenshot, **kwargs):
        calls.update(url=url, resume=resume, screenshot=screenshot)
        screenshot.write_bytes(b"\x89PNG fake")
        return FillResult(ok=True, steps=7, screenshot=screenshot,
                          notes="filled every field", **overrides)

    monkeypatch.setattr(apply.filler, "fill", fake)
    return calls


def test_filling_stops_at_filled(monkeypatch):
    """FILLED is terminal for the agent. Nothing may reach SUBMITTED."""
    job, app_dir = approved_job()
    stub_fill(monkeypatch)

    assert apply.fill_one(job) is True
    assert queue.get(job.id).status == Status.FILLED
    assert store.read_status(app_dir) == "filled"
    assert (app_dir / "fill_screenshot.png").exists()
    assert (app_dir / "fill_notes.md").exists()


@pytest.mark.parametrize("status", [
    Status.QUEUED, Status.AWAITING_REVIEW, Status.SKIPPED, Status.FAILED, Status.FILLED,
])
def test_an_unapproved_job_is_never_filled(monkeypatch, status):
    """Checkpoint 1 cannot be bypassed by running apply.py directly."""
    job, _ = approved_job(status)
    calls = stub_fill(monkeypatch)

    assert apply.fill_one(job) is False
    assert calls == {}, "the browser must not be opened for an unapproved job"
    assert queue.get(job.id).status == status


def test_the_tailored_resume_is_what_gets_uploaded(monkeypatch):
    job, app_dir = approved_job()
    calls = stub_fill(monkeypatch)
    apply.fill_one(job)
    assert calls["resume"] == app_dir / "resume.pdf"
    assert calls["url"] == job.url


def test_blocked_submit_attempts_are_recorded(monkeypatch):
    job, app_dir = approved_job()
    stub_fill(monkeypatch, blocked_attempts=2)
    apply.fill_one(job)
    assert "blocked 2 submit attempt(s)" in (app_dir / "fill_notes.md").read_text()


def test_a_browser_failure_marks_the_job_and_keeps_the_folder(monkeypatch):
    job, app_dir = approved_job()
    monkeypatch.setattr(apply.filler, "fill",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("chrome died")))

    assert apply.fill_one(job) is False
    assert queue.get(job.id).status == Status.FAILED
    assert "chrome died" in queue.get(job.id).error
    assert app_dir.exists()


def test_a_missing_resume_stops_the_run(monkeypatch):
    job, app_dir = approved_job()
    (app_dir / "resume.pdf").unlink()
    calls = stub_fill(monkeypatch)
    assert apply.fill_one(job) is False
    assert calls == {}


def test_main_reports_when_nothing_is_approved(capsys):
    approved_job(Status.AWAITING_REVIEW)
    assert apply.main(["apply.py"]) == 0
    assert "nothing approved" in capsys.readouterr().out
