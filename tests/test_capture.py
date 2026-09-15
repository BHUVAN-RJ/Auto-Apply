"""Capturing a job starts the pipeline; nothing past checkpoint 1 starts."""

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
def launched(monkeypatch):
    calls = []
    monkeypatch.setattr(runner, "launch", lambda script, job_id, log_dir=None: calls.append((script, job_id)) or 4242)
    return calls


def test_capture_starts_the_pipeline_at_once(launched):
    client = TestClient(app)
    body = client.post("/capture", json={"url": "https://example.com/jobs/9", "title": "SWE"}).json()
    assert body["created"] and body["processing"]
    assert launched == [("pipeline.py", body["id"])]


def test_capture_never_starts_the_fill(launched):
    client = TestClient(app)
    client.post("/capture", json={"url": "https://example.com/jobs/9"})
    assert all(script == "pipeline.py" for script, _ in launched)


def test_recapturing_a_queued_url_starts_it_but_a_processed_one_is_left_alone(launched):
    client = TestClient(app)
    first = client.post("/capture", json={"url": "https://example.com/jobs/9"}).json()
    again = client.post("/capture", json={"url": "https://example.com/jobs/9"}).json()
    assert not again["created"] and again["processing"]
    assert launched == [("pipeline.py", first["id"])] * 2

    queue.update(first["id"], status=Status.AWAITING_REVIEW)
    third = client.post("/capture", json={"url": "https://example.com/jobs/9"}).json()
    assert not third["processing"] and len(launched) == 2


def test_process_now_only_for_jobs_before_checkpoint_one(launched):
    client = TestClient(app)
    job, _ = queue.add(Job(url="https://example.com/jobs/9"))
    assert client.post(f"/jobs/{job.id}/process").json()["pid"] == 4242

    queue.update(job.id, status=Status.FAILED)
    assert client.post(f"/jobs/{job.id}/process").status_code == 200

    queue.update(job.id, status=Status.APPROVED)
    assert client.post(f"/jobs/{job.id}/process").status_code == 409
    assert client.post("/jobs/zzzz/process").status_code == 404
    assert launched == [("pipeline.py", job.id)] * 2


def test_the_page_no_longer_tells_the_user_to_run_a_command():
    html = (runner.ROOT / "review" / "index.html").read_text()
    assert "Run <code>python pipeline.py" not in html
    assert "Tailor it now" in html
