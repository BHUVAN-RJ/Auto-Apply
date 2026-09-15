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
        calls.update(url=url, resume=resume, screenshot=screenshot, **kwargs)
        screenshot.write_bytes(b"\x89PNG fake")
        overrides.setdefault("done", True)
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
    monkeypatch.setenv(apply.RESUME_FILENAME, "Jane Q Doe Resume")
    job, app_dir = approved_job()
    calls = stub_fill(monkeypatch)
    apply.fill_one(job)
    assert calls["resume"] == app_dir / "Jane_Q_Doe_Resume.pdf"
    assert calls["resume"].read_bytes() == (app_dir / "resume.pdf").read_bytes()
    assert calls["url"] == job.url


@pytest.mark.parametrize("raw,expected", [
    ("Jane Q Doe Resume", "Jane_Q_Doe_Resume"),
    ("Jane_Doe_Resume.pdf", "Jane_Doe_Resume"),
    ("  ", "Resume"),
    ("../evil name", "evil_name"),
])
def test_resume_filename_uses_underscores_and_nothing_unsafe(monkeypatch, raw, expected):
    monkeypatch.setenv(apply.RESUME_FILENAME, raw)
    assert apply.resume_filename() == expected


def test_the_upload_copy_is_made_once(monkeypatch):
    monkeypatch.setenv(apply.RESUME_FILENAME, "Jane_Doe_Resume")
    _, app_dir = approved_job()
    first = apply.upload_copy(app_dir)
    first.write_bytes(b"%PDF-1.5 already there")
    assert apply.upload_copy(app_dir).read_bytes() == b"%PDF-1.5 already there"


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


def test_a_run_that_errored_on_every_step_is_not_reported_as_filled(monkeypatch):
    """A screenshot proves the browser was alive, not that the form was filled."""
    job, app_dir = approved_job()

    def fake(url, resume, screenshot, **kwargs):
        screenshot.write_bytes(b"\x89PNG fake")
        return FillResult(ok=False, steps=6, screenshot=screenshot,
                          notes="", done=False,
                          errors=["404 - No endpoints found that support image input"] * 6)

    monkeypatch.setattr(apply.filler, "fill", fake)

    assert apply.fill_one(job) is False
    assert queue.get(job.id).status == Status.FAILED
    assert "image input" in queue.get(job.id).error
    notes = (app_dir / "fill_notes.md").read_text()
    assert "image input" in notes, "the reviewer must be able to see what broke"
    assert f"data/apply_{job.id}.log" in notes, "and where to read more"


def test_a_successful_run_records_the_log_path_too(monkeypatch):
    job, app_dir = approved_job()
    stub_fill(monkeypatch, done=True)
    assert apply.fill_one(job) is True
    assert f"data/apply_{job.id}.log" in (app_dir / "fill_notes.md").read_text()


def test_the_cover_letter_is_uploaded_under_its_own_name(monkeypatch):
    monkeypatch.setenv(apply.COVER_LETTER_FILENAME, "Jane Doe cover letter")
    job, app_dir = approved_job()
    store.write(app_dir, "cover_letter.pdf", b"%PDF-1.5 letter")
    calls = stub_fill(monkeypatch)
    apply.fill_one(job)
    assert calls["cover_letter_pdf"] == app_dir / "Jane_Doe_cover_letter.pdf"
    assert calls["cover_letter_pdf"].read_bytes() == b"%PDF-1.5 letter"


def test_no_cover_letter_means_none_is_offered(monkeypatch):
    job, app_dir = approved_job()
    calls = stub_fill(monkeypatch)
    apply.fill_one(job)
    assert calls["cover_letter_pdf"] is None
    assert not list(app_dir.glob("*over*")), "no stray copy without a source"


def test_errors_are_condensed_to_distinct_one_liners():
    long = ("File path /x/resume.pdf is not available. To fix: The user must add this file "
            "path to the available_file_paths parameter. Example: Agent(...)")
    out = apply.condensed([long, long, long, "Element 4 does not exist.\nline two"])
    assert out == ["File path /x/resume.pdf is not available. (x3)",
                   "Element 4 does not exist. line two"]


def test_condensed_caps_the_list_and_width():
    out = apply.condensed([f"error {i} " + "x" * 300 for i in range(8)], limit=3, width=40)
    assert len(out) == 4 and out[-1] == "… and 5 more, see the log"
    assert all(len(line) <= 40 for line in out[:3])


def test_fill_notes_carry_condensed_errors(monkeypatch):
    job, app_dir = approved_job()
    stub_fill(monkeypatch, errors=["same thing. To fix: blah"] * 4, resume_uploaded=True)
    apply.fill_one(job)
    notes = (app_dir / "fill_notes.md").read_text()
    assert notes.count("same thing.") == 1 and "(x4)" in notes and "To fix" not in notes


def test_answers_are_archived_and_counted(monkeypatch):
    from tailor import answers

    job, app_dir = approved_job()
    calls = stub_fill(monkeypatch, resume_uploaded=True)

    def fake_fill(url, resume, screenshot, **kwargs):
        kwargs["answerer"].answers.append(answers.Answer("Why here?", "Because.", "m"))
        return apply.filler.FillResult(ok=True, steps=3, screenshot=None, notes="", done=True,
                                       resume_uploaded=True)

    monkeypatch.setattr(apply.filler, "fill", fake_fill)
    apply.fill_one(job)
    assert "**Why here?**" in (app_dir / "answers.md").read_text()
    assert "1 question(s) answered" in (app_dir / "fill_notes.md").read_text()
