"""Checkpoint 1 API: artifact serving and the approve/reject gate."""

import pytest
from fastapi.testclient import TestClient

from archive import store
from server import queue, runner
from server.app import app
from server.models import Job, Status


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    qpath = tmp_path / "queue.json"
    monkeypatch.setattr(queue, "QUEUE_PATH", qpath)
    monkeypatch.setattr(queue, "LOCK_PATH", qpath.with_suffix(".lock"))
    monkeypatch.setattr(store, "APPLICATIONS", tmp_path / "applications")
    monkeypatch.setattr(store, "INDEX_PATH", tmp_path / "applications" / "index.csv")

    import server.review as review
    monkeypatch.setattr(review, "DATA_DIR", tmp_path / "data")

    # .env on a real machine may switch autofill on; a test approving a job
    # must never launch the real apply.py against a temporary queue.
    monkeypatch.setattr(runner, "AUTOFILL_ENABLED", False)
    monkeypatch.setattr(runner, "_fills", {})
    monkeypatch.setattr(runner, "_pgrep_fill", lambda job_id: None)
    # Never touch a real Chrome from a test.
    import server.review as review_module
    monkeypatch.setattr(review_module.chrome, "close",
                        lambda port_number=None: pytest.fail("a test closed Chrome"))
    monkeypatch.setattr(review_module.chrome, "close_tab", lambda target_id, port_number=None: False)
    from browser import forms
    monkeypatch.setattr(forms, "find_target", lambda cdp, url, target_id="": "")
    # ...nor look at its tabs: the correction loop's snapshot lists them.
    from server import corrections
    monkeypatch.setattr(corrections, "capture", lambda job_url, app_dir: {"captured": False, "reason": "test"})


@pytest.fixture
def client():
    return TestClient(app)


def reviewable_job() -> tuple[str, object]:
    """A job that has been through the pipeline and is awaiting review."""
    job, _ = queue.add(Job(url="https://example.com/jobs/1", title="Backend Engineer",
                           company="Example Corp"))
    app_dir = store.create(job)
    store.write(app_dir, "resume.diff", "--- a/resume.tex\n+++ b/resume.tex\n+tailored\n")
    store.write(app_dir, "suggestions.md", "- reordered the projects")
    store.write(app_dir, "resume.pdf", b"%PDF-1.5 fake")
    store.set_status(app_dir, Status.AWAITING_REVIEW)
    queue.update(job.id, status=Status.AWAITING_REVIEW, app_dir=str(app_dir))
    return job.id, app_dir


def test_detail_returns_the_artifacts(client):
    job_id, app_dir = reviewable_job()
    data = client.get(f"/review/{job_id}").json()
    assert data["status"] == "awaiting_review"
    assert data["folder"] == app_dir.name
    assert "+tailored" in data["diff"]
    assert "reordered" in data["suggestions"]
    assert "resume.pdf" in data["artifacts"]


def test_detail_carries_the_cover_letter_and_its_pdf(client):
    job_id, app_dir = reviewable_job()
    store.write(app_dir, "cover_letter.md", "Dear Hiring Manager,\n\nHi.\n\nSincerely,\nJane\n")
    store.write(app_dir, "cover_letter.pdf", b"%PDF-1.5 letter")
    data = client.get(f"/review/{job_id}").json()
    assert data["cover_letter"].startswith("Dear Hiring Manager")
    assert "cover_letter.pdf" in data["artifacts"]
    pdf = client.get(f"/review/{job_id}/file/cover_letter.pdf")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")


def test_detail_surfaces_a_failed_cover_letter(client):
    job_id, app_dir = reviewable_job()
    store.write(app_dir, "cover_letter_error.txt", "LLMError: no content\n")
    data = client.get(f"/review/{job_id}").json()
    assert "no content" in data["cover_letter_error"]
    assert data["cover_letter"] == ""


def test_detail_shows_only_the_latest_fill_notes(client):
    job_id, app_dir = reviewable_job()
    store.write_or_append(app_dir, "fill_notes.md", "# Fill notes\n\nfirst run, failed\n")
    store.write_or_append(app_dir, "fill_notes.md", "# Fill notes\n\nsecond run, filled\n")
    data = client.get(f"/review/{job_id}").json()
    assert "second run" in data["fill_notes"] and "first run" not in data["fill_notes"]
    assert (app_dir / "fill_notes.md").read_text().count("Fill notes") == 2, "the file keeps both"


def test_detail_404s_on_an_unknown_job(client):
    assert client.get("/review/zzzz").status_code == 404


def test_detail_409s_before_the_pipeline_has_run(client):
    job, _ = queue.add(Job(url="https://example.com/jobs/1"))
    assert client.get(f"/review/{job.id}").status_code == 409


def test_serves_a_pdf_artifact(client):
    job_id, _ = reviewable_job()
    response = client.get(f"/review/{job_id}/file/resume.pdf")
    assert response.status_code == 200
    assert response.content.startswith(b"%PDF")


def test_refuses_an_artifact_outside_the_allow_list(client):
    """A crafted name must not walk out of the application folder."""
    job_id, _ = reviewable_job()
    for name in ("status.json", "..%2F..%2F.env", "job.json"):
        assert client.get(f"/review/{job_id}/file/{name}").status_code == 404


def test_approve_moves_the_job_and_records_it(client):
    job_id, app_dir = reviewable_job()
    assert client.post(f"/review/{job_id}/approve", json={}).json()["status"] == "approved"
    assert queue.get(job_id).status == Status.APPROVED
    assert store.read_status(app_dir) == "approved"


def test_approve_is_rejected_when_not_awaiting_review(client):
    """The gate cannot be opened twice, or skipped."""
    job_id, _ = reviewable_job()
    client.post(f"/review/{job_id}/approve", json={})
    second = client.post(f"/review/{job_id}/approve", json={})
    assert second.status_code == 409


def test_approve_cannot_be_reached_from_queued(client):
    job, _ = queue.add(Job(url="https://example.com/jobs/1"))
    app_dir = store.create(job)
    queue.update(job.id, app_dir=str(app_dir))
    assert client.post(f"/review/{job.id}/approve", json={}).status_code == 409


def test_reject_records_the_reason_and_keeps_the_folder(client):
    job_id, app_dir = reviewable_job()
    response = client.post(f"/review/{job_id}/reject",
                           json={"reason": "seniority", "note": "wants 8 years"})

    assert response.status_code == 200
    job = queue.get(job_id)
    assert job.status == Status.SKIPPED
    assert job.reject_reason.value == "seniority"
    assert job.reject_note == "wants 8 years"
    assert "Wrong seniority" in (app_dir / "rejection.md").read_text()
    assert "wants 8 years" in (app_dir / "rejection.md").read_text()
    assert (app_dir / "resume.diff").exists(), "the record survives a rejection"


def test_reject_requires_a_reason(client):
    """A rejection with no reason tells you nothing weeks later."""
    job_id, _ = reviewable_job()
    assert client.post(f"/review/{job_id}/reject", json={}).status_code == 422
    assert client.post(f"/review/{job_id}/reject", json={"note": "nope"}).status_code == 422
    assert queue.get(job_id).status == Status.AWAITING_REVIEW


def test_reject_refuses_an_unknown_reason(client):
    job_id, _ = reviewable_job()
    assert client.post(f"/review/{job_id}/reject",
                       json={"reason": "vibes"}).status_code == 422


def test_a_reason_without_a_note_is_enough(client):
    job_id, app_dir = reviewable_job()
    client.post(f"/review/{job_id}/reject", json={"reason": "changed_my_mind"})
    assert queue.get(job_id).reject_reason.value == "changed_my_mind"
    assert "no further detail" in (app_dir / "rejection.md").read_text()


def test_the_rejection_reason_comes_back_on_the_detail(client):
    job_id, _ = reviewable_job()
    client.post(f"/review/{job_id}/reject", json={"reason": "location", "note": "onsite NYC"})
    data = client.get(f"/review/{job_id}").json()
    assert data["reject_label"] == "Location or work authorisation"
    assert data["reject_note"] == "onsite NYC"


def test_changing_your_mind_twice_appends_rather_than_collides(client):
    """The reviewer's decisions are part of the record, so they accumulate."""
    job_id, app_dir = reviewable_job()
    client.post(f"/review/{job_id}/reject", json={"reason": "poor_fit", "note": "first call"})
    client.post(f"/review/{job_id}/reject", json={"reason": "location", "note": "second call"})
    text = (app_dir / "rejection.md").read_text()
    assert "first call" in text and "second call" in text


def test_the_reason_list_is_served_to_the_page(client):
    reasons = client.get("/review/meta/reject-reasons").json()
    assert {"value": "poor_fit", "label": "Not a good fit"} in reasons
    assert len(reasons) == 7


def test_revise_reruns_the_pipeline_with_the_instruction(client, monkeypatch):
    job_id, first_dir = reviewable_job()
    seen = {}

    def fake_retailor(job, instruction):
        seen["instruction"] = instruction
        return first_dir.parent / "second-folder"

    import pipeline
    monkeypatch.setattr(pipeline, "retailor", fake_retailor)

    response = client.post(f"/review/{job_id}/revise",
                           json={"instruction": "lead with distributed systems"})
    assert response.status_code == 200
    assert seen["instruction"] == "lead with distributed systems"


def test_revise_rejects_an_empty_instruction(client):
    job_id, _ = reviewable_job()
    assert client.post(f"/review/{job_id}/revise", json={"instruction": "  "}).status_code == 400


def test_index_page_is_served(client):
    assert "Job Autopilot" in client.get("/").text


def test_the_page_opens_on_the_first_job_in_flight_or_says_nothing_is():
    """Never a job from a closed shelf by default; the URL's #<id> (the
    capture's "Open in autopilot") wins when it names one."""
    script = (runner.ROOT / "review" / "index.html").read_text()
    assert "if (inflight.length) show(inflight[0].id);" in script
    assert "Nothing in flight." in script
    assert "show((waiting[0] || jobs[0]).id)" not in script
    assert 'let current = location.hash.length > 1 ? location.hash.slice(1) : null;' in script
    assert 'window.addEventListener("hashchange"' in script


def filled_job() -> tuple[str, object]:
    job_id, app_dir = reviewable_job()
    store.write(app_dir, "fill_screenshot.png", b"\x89PNG fake")
    store.write(app_dir, "fill_notes.md", "- filled every field")
    store.set_status(app_dir, Status.FILLED)
    queue.update(job_id, status=Status.FILLED)
    return job_id, app_dir


def test_a_human_can_mark_a_filled_job_submitted(client, monkeypatch):
    import server.review as review_module
    monkeypatch.setattr(review_module.chrome, "close", lambda port_number=None: True)
    job_id, app_dir = filled_job()
    assert client.post(f"/review/{job_id}/submitted", json={}).json()["status"] == "submitted"
    assert queue.get(job_id).status == Status.SUBMITTED
    assert store.read_status(app_dir) == "submitted"


def test_submitted_cannot_be_reached_before_the_form_is_filled(client):
    """The agent halts at FILLED; nothing may jump the second checkpoint."""
    job_id, _ = reviewable_job()
    assert client.post(f"/review/{job_id}/submitted", json={}).status_code == 409


def test_the_fill_screenshot_is_served(client):
    job_id, _ = filled_job()
    response = client.get(f"/review/{job_id}/file/fill_screenshot.png")
    assert response.status_code == 200 and response.content.startswith(b"\x89PNG")


def test_you_can_reject_at_any_stage(client):
    """Control stays with the human: an approved or filled job can still be dropped."""
    for status in (Status.APPROVED, Status.FILLED, Status.FAILED):
        queue.QUEUE_PATH.unlink(missing_ok=True)
        job_id, _ = reviewable_job()
        queue.update(job_id, status=status)
        response = client.post(f"/review/{job_id}/reject",
                               json={"reason": "changed_my_mind"})
        assert response.status_code == 200, status
        assert queue.get(job_id).status == Status.SKIPPED


def test_the_job_list_carries_the_rejection_reason(client):
    """The sidebar shows why something was rejected without opening it."""
    job_id, _ = reviewable_job()
    client.post(f"/review/{job_id}/reject", json={"reason": "location", "note": "onsite NYC"})
    row = next(j for j in client.get("/jobs").json() if j["id"] == job_id)
    assert row["reject_reason"] == "location"
    assert row["reject_note"] == "onsite NYC"


def test_approving_does_not_launch_the_browser_by_default(client, monkeypatch):
    """Filling is started explicitly, so a run can be repeated at will."""
    job_id, _ = reviewable_job()
    monkeypatch.setattr(runner, "AUTOFILL_ENABLED", False)
    monkeypatch.setattr(runner.subprocess, "Popen",
                        lambda *a, **k: pytest.fail("approval must not launch a browser"))

    response = client.post(f"/review/{job_id}/approve", json={})

    assert response.json()["filling"] is False
    assert queue.get(job_id).status == Status.APPROVED


def test_approving_starts_the_fill_when_autofill_is_switched_on(client, monkeypatch):
    job_id, _ = reviewable_job()
    started = {}

    def fake_start(job_id):
        started["id"] = job_id
        return 99

    monkeypatch.setattr(runner, "start_fill", fake_start)
    assert client.post(f"/review/{job_id}/approve", json={}).json()["filling"] is True
    assert started["id"] == job_id


def test_approving_still_works_when_autofill_is_off(client, monkeypatch):
    job_id, _ = reviewable_job()
    monkeypatch.setattr(runner, "start_fill", lambda jid: None)
    response = client.post(f"/review/{job_id}/approve", json={})
    assert response.json() == {"id": job_id, "status": "approved", "filling": False,
                               "pid": None}


def test_rejecting_never_starts_the_fill(client, monkeypatch):
    job_id, _ = reviewable_job()
    monkeypatch.setattr(runner, "start_fill",
                        lambda jid: pytest.fail("a rejected job must not be filled"))
    client.post(f"/review/{job_id}/reject", json={"reason": "poor_fit"})
    assert queue.get(job_id).status == Status.SKIPPED


def test_fill_now_requires_an_approved_job(client, monkeypatch):
    monkeypatch.setattr(runner, "launch", lambda script, jid: 77)
    job_id, _ = reviewable_job()
    assert client.post(f"/review/{job_id}/fill", json={}).status_code == 409

    queue.update(job_id, status=Status.APPROVED)
    assert client.post(f"/review/{job_id}/fill", json={}).json()["pid"] == 77


@pytest.mark.parametrize("status", [Status.APPROVED, Status.FILLING, Status.FILLED,
                                    Status.FAILED])
def test_a_fill_can_always_be_restarted(client, monkeypatch, status):
    """A run whose browser died must not wedge the job."""
    monkeypatch.setattr(runner, "launch", lambda script, jid: 77)
    job_id, _ = reviewable_job()
    queue.update(job_id, status=status)

    assert client.post(f"/review/{job_id}/fill", json={}).status_code == 200
    assert queue.get(job_id).status == Status.APPROVED, (
        "a restart must leave the job in a state apply.py will pick up")


def test_the_fill_log_is_readable_from_the_page(client, tmp_path, monkeypatch):
    """A failed fill must be diagnosable without going to the terminal."""
    job_id, _ = reviewable_job()
    log_dir = tmp_path / "data"
    log_dir.mkdir()
    (log_dir / f"apply_{job_id}.log").write_text("\n".join(f"line {n}" for n in range(200)))

    body = client.get(f"/review/{job_id}/log?lines=10").text
    assert "line 199" in body
    assert "line 100" not in body, "only the tail is served"


def test_no_log_yet_is_a_404_not_an_error(client):
    job_id, _ = reviewable_job()
    assert client.get(f"/review/{job_id}/log").status_code == 404


def test_the_failure_reason_reaches_the_detail(client):
    job_id, _ = reviewable_job()
    queue.update(job_id, status=Status.FAILED, error="fill did not complete: 404 no image support")
    data = client.get(f"/review/{job_id}").json()
    assert "404 no image support" in data["error_detail"]


class LiveFill:
    """Stands in for a Popen that has not exited."""

    def __init__(self, pid: int) -> None:
        self.pid = pid

    def poll(self):
        return None


def test_fill_is_refused_while_one_is_running(client, monkeypatch):
    """One fill per job: a second apply.py on the same tab undoes the first."""
    monkeypatch.setattr(runner, "launch",
                        lambda script, jid: pytest.fail("must not start a second fill"))
    job_id, _ = reviewable_job()
    queue.update(job_id, status=Status.FILLING)
    runner._fills[job_id] = (LiveFill(4242), 0.0)

    response = client.post(f"/review/{job_id}/fill", json={})

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "error": "fill_running", "pid": 4242,
        "message": "a fill is already running for this job (pid 4242)"}


def test_forced_fill_kills_the_running_one_first(client, monkeypatch):
    """The page sends force only after the human confirmed the kill."""
    order = []
    monkeypatch.setattr(runner, "stop_fill", lambda jid: order.append(("stop", jid)) or 4242)
    monkeypatch.setattr(runner, "launch", lambda script, jid: order.append(("start", jid)) or 77)
    job_id, _ = reviewable_job()
    queue.update(job_id, status=Status.FILLING)
    runner._fills[job_id] = (LiveFill(4242), 0.0)

    response = client.post(f"/review/{job_id}/fill", json={"force": True})

    assert response.status_code == 200
    assert response.json()["pid"] == 77
    assert order == [("stop", job_id), ("start", job_id)]


def test_detail_reports_the_running_fill(client, monkeypatch):
    job_id, _ = reviewable_job()
    queue.update(job_id, status=Status.FILLING)
    assert client.get(f"/review/{job_id}").json()["fill"] is None

    monkeypatch.setattr(runner.time, "time", lambda: 1000.0)
    runner._fills[job_id] = (LiveFill(4242), 990.0)
    fill = client.get(f"/review/{job_id}").json()["fill"]
    assert fill == {"pid": 4242, "started_at": 990.0, "age": 10}


def test_submitting_closes_only_the_jobs_tab_never_the_browser(client, monkeypatch):
    """The browser is the person's working set (the Jobright list, the next
    form, the logins); only the submitted form's tab goes. Closing the
    whole browser read as a crash."""
    import json
    import server.review as review_module
    closed = []
    monkeypatch.setattr(review_module.chrome, "close_tab",
                        lambda target_id, port_number=None: closed.append(target_id) or True)
    job_id, app_dir = filled_job()
    store.write(app_dir, "form_fill.json", json.dumps({"target_id": "TAB1234", "after_agent": {}}))
    from browser import forms
    monkeypatch.setattr(forms, "find_target", lambda cdp, url, target_id="": target_id)

    result = client.post(f"/review/{job_id}/submitted", json={}).json()

    assert result["status"] == "submitted" and result["tab"] == "closed"
    assert closed == ["TAB1234"]
    assert "Browser.close" not in open(review_module.__file__).read()


def test_submitting_with_the_tab_already_gone_still_marks_it(client, monkeypatch):
    import server.review as review_module
    monkeypatch.setattr(review_module.chrome, "close_tab", lambda target_id, port_number=None: False)
    from browser import forms
    monkeypatch.setattr(forms, "find_target", lambda cdp, url, target_id="": "")
    job_id, _ = filled_job()
    result = client.post(f"/review/{job_id}/submitted", json={}).json()
    assert result["status"] == "submitted" and result["tab"] == "not_open"


def test_submitting_while_filling_stops_the_agent(client, monkeypatch):
    """The human's submission wins over an agent still poking at the form."""
    import server.review as review_module
    monkeypatch.setattr(review_module.chrome, "close", lambda port_number=None: True)
    stopped = []
    monkeypatch.setattr(runner, "stop_fill", lambda jid: stopped.append(jid) or 4242)
    job_id, _ = reviewable_job()
    queue.update(job_id, status=Status.FILLING)

    result = client.post(f"/review/{job_id}/submitted", json={}).json()

    assert result["status"] == "submitted"
    assert stopped == [job_id]
    assert queue.get(job_id).status == Status.SUBMITTED


def test_submitting_needs_a_form_in_the_browser(client):
    job_id, _ = reviewable_job()
    assert client.post(f"/review/{job_id}/submitted", json={}).status_code == 409



def test_confirmation_seen_marks_submitted_without_closing_the_browser(client, monkeypatch):
    import server.review as review_module
    closed = []
    monkeypatch.setattr(review_module.chrome, "close", lambda port_number=None: closed.append(1) or True)
    job_id, app_dir = filled_job()
    body = client.post(f"/review/{job_id}/submitted-seen",
                       json={"url": "https://x.com/thanks", "quote": "Thank you for applying"}).json()
    assert body["marked"] and body["status"] == "submitted"
    assert queue.get(job_id).status == Status.SUBMITTED
    assert closed == []
    assert "confirmation seen on the page" in (app_dir / "status.json").read_text()


def test_confirmation_seen_ignores_jobs_not_in_the_browser(client):
    job_id, _ = reviewable_job()
    body = client.post(f"/review/{job_id}/submitted-seen", json={"quote": "Thank you for applying"}).json()
    assert body["marked"] is False
    assert queue.get(job_id).status == Status.AWAITING_REVIEW


def test_the_page_can_fetch_the_tailored_files_under_their_upload_names(client, monkeypatch):
    import base64
    monkeypatch.setenv("AUTOPILOT_RESUME_FILENAME", "Jane Doe Resume")
    monkeypatch.setenv("AUTOPILOT_COVER_LETTER_FILENAME", "Jane Doe Cover Letter")
    job_id, app_dir = reviewable_job()
    store.write(app_dir, "cover_letter.pdf", b"%PDF-letter")
    body = client.post(f"/review/{job_id}/files").json()
    assert body["resume"]["name"] == "Jane_Doe_Resume.pdf"
    assert body["cover_letter"]["name"] == "Jane_Doe_Cover_Letter.pdf"
    assert base64.b64decode(body["cover_letter"]["b64"]) == b"%PDF-letter"
    assert body["resume"]["type"] == "application/pdf"


# -- /autofilled: the trigger after Jobright's autofill --------------------

def autofilled_setup(monkeypatch, tmp_path):
    from server import settings
    monkeypatch.setattr(settings, "SETTINGS_PATH", tmp_path / "settings.json")
    settings.save(auto_fill=True)
    launched = []
    monkeypatch.setattr(runner, "launch", lambda script, job_id, log_dir=None: launched.append((script, job_id)) or 777)
    monkeypatch.setattr(runner, "start_pipeline", lambda job_id, log_dir=None: launched.append(("pipeline.py", job_id)) or 778)
    return launched


def test_autofilled_restarts_the_fill_of_a_job_that_failed_mid_fill(client, monkeypatch, tmp_path):
    launched = autofilled_setup(monkeypatch, tmp_path)
    job_id, app_dir = reviewable_job()
    queue.update(job_id, status=Status.FAILED, error="agent died")
    result = client.post("/autofilled", json={"url": "https://example.com/jobs/1?utm=x", "note": "9/10"}).json()
    assert result == {"action": "fill", "id": job_id, "pid": 777}
    assert launched == [("apply.py", job_id)]
    assert queue.get(job_id).status == Status.APPROVED and queue.get(job_id).error is None


def test_autofilled_never_doubles_a_running_fill(client, monkeypatch, tmp_path):
    launched = autofilled_setup(monkeypatch, tmp_path)
    job_id, _ = reviewable_job()
    queue.update(job_id, status=Status.FILLING)
    monkeypatch.setattr(runner, "fill_pid", lambda jid: 4242)
    result = client.post("/autofilled", json={"url": "https://example.com/jobs/1"}).json()
    assert result["action"] == "running" and launched == []


def test_autofilled_waits_for_the_pipeline_and_ignores_unknown_and_closed_jobs(client, monkeypatch, tmp_path):
    launched = autofilled_setup(monkeypatch, tmp_path)
    job_id, _ = reviewable_job()   # awaiting review: the pipeline's, or the human's
    assert client.post("/autofilled", json={"url": "https://example.com/jobs/1"}).json()["action"] == "waiting"
    queue.update(job_id, status=Status.SUBMITTED)
    assert client.post("/autofilled", json={"url": "https://example.com/jobs/1"}).json()["action"] == "waiting"
    assert client.post("/autofilled", json={"url": "https://example.com/jobs/other"}).json()["action"] == "none"
    assert launched == []


def test_autofilled_starts_the_pipeline_for_a_job_that_never_got_a_resume(client, monkeypatch, tmp_path):
    launched = autofilled_setup(monkeypatch, tmp_path)
    job, _ = queue.add(Job(url="https://example.com/jobs/9", title="X"))
    queue.update(job.id, status=Status.FAILED, error="fetch failed")
    result = client.post("/autofilled", json={"url": "https://example.com/jobs/9"}).json()
    assert result["action"] == "pipeline" and launched == [("pipeline.py", job.id)]


def test_autofilled_does_nothing_with_the_switch_off(client, monkeypatch, tmp_path):
    from server import settings
    launched = autofilled_setup(monkeypatch, tmp_path)
    settings.save(auto_fill=False)
    job_id, _ = reviewable_job()
    queue.update(job_id, status=Status.FAILED)
    assert client.post("/autofilled", json={"url": "https://example.com/jobs/1"}).json()["action"] == "waiting"
    assert launched == []


def test_ask_answers_from_the_application_and_keeps_it(client, monkeypatch):
    """A free-form question pasted on the review page is answered from this
    application's own material and appended to answers.md; nothing is typed
    into any form."""
    import json
    from server import review as review_module
    job_id, app_dir = reviewable_job()
    store.write(app_dir, "posting.md", "Perplexity builds an answer engine.")
    store.write(app_dir, "resume.tex", "\\documentclass{article} built search at Acme")
    store.write(app_dir, "cover_letter.md", "Dear team, search.")
    store.write(app_dir, "form_fill.json", json.dumps({"after_agent": {
        "First name": "Jane",
        "What are the most interesting aspects of Perplexity that you are excited to work on?": "",
        "Will you now or in the future require sponsorship?": ""}}))
    seen = {}

    def fake(question, context, model=None):
        seen["question"], seen["context"] = question, context
        return review_module.answers.Answer(question, "Search ranking, because I built it at Acme.", "test/model")
    monkeypatch.setattr(review_module.answers, "answer", fake)
    monkeypatch.setattr(review_module.tailor, "load_profile", lambda stories=None: "profile text")
    monkeypatch.setattr(review_module.profile, "applicant_facts", lambda: "facts text")

    detail = client.get(f"/review/{job_id}").json()
    assert detail["questions"] == ["What are the most interesting aspects of Perplexity that you are excited to work on?"]

    r = client.post(f"/review/{job_id}/ask", json={"question": "  What excites you\nabout Perplexity? "})
    assert r.status_code == 200 and r.json()["answer"].startswith("Search ranking")
    assert seen["question"] == "What excites you about Perplexity?"
    for piece in ("Perplexity builds", "built search at Acme", "Dear team", "profile text", "facts text"):
        assert piece in seen["context"].as_message()
    kept = (app_dir / "answers.md").read_text()
    assert "What excites you about Perplexity?" in kept and "Search ranking" in kept
    assert "Search ranking" in client.get(f"/review/{job_id}").json()["answers"]

    assert client.post(f"/review/{job_id}/ask", json={"question": "  "}).status_code == 400


def test_ask_refuses_a_visa_question(client, monkeypatch):
    from server import review as review_module
    job_id, _ = reviewable_job()
    monkeypatch.setattr(review_module.answers.llm, "complete",
                        lambda *a, **k: pytest.fail("a visa question reached the model"))
    monkeypatch.setattr(review_module.tailor, "load_profile", lambda stories=None: "")
    monkeypatch.setattr(review_module.profile, "applicant_facts", lambda: "")
    r = client.post(f"/review/{job_id}/ask", json={"question": "Do you require visa sponsorship?"})
    assert r.status_code == 422
