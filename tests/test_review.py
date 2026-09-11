"""Checkpoint 1 API: artifact serving and the approve/reject gate."""

import pytest
from fastapi.testclient import TestClient

from archive import store
from server import queue
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


def test_reject_skips_the_job_and_keeps_the_folder(client):
    job_id, app_dir = reviewable_job()
    client.post(f"/review/{job_id}/reject", json={"note": "too senior"})
    assert queue.get(job_id).status == Status.SKIPPED
    assert store.read_status(app_dir) == "skipped"
    assert (app_dir / "resume.diff").exists(), "the record survives a rejection"


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
