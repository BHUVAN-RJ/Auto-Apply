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


def filled_job() -> tuple[str, object]:
    job_id, app_dir = reviewable_job()
    store.write(app_dir, "fill_screenshot.png", b"\x89PNG fake")
    store.write(app_dir, "fill_notes.md", "- filled every field")
    store.set_status(app_dir, Status.FILLED)
    queue.update(job_id, status=Status.FILLED)
    return job_id, app_dir


def test_a_human_can_mark_a_filled_job_submitted(client):
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


def test_approving_starts_the_fill(client, monkeypatch):
    """Approval is the consent; making the human then run a command adds nothing."""
    job_id, _ = reviewable_job()
    started = {}
    def fake_start(job_id):
        started["id"] = job_id
        return 99

    monkeypatch.setattr(runner, "start_fill", fake_start)

    response = client.post(f"/review/{job_id}/approve", json={})

    assert response.json()["filling"] is True
    assert started["id"] == job_id
    assert queue.get(job_id).status == Status.APPROVED


def test_approving_still_works_when_autofill_is_off(client, monkeypatch):
    job_id, _ = reviewable_job()
    monkeypatch.setattr(runner, "start_fill", lambda jid: None)
    response = client.post(f"/review/{job_id}/approve", json={})
    assert response.json() == {"id": job_id, "status": "approved", "filling": False}


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
