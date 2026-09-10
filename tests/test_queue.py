"""Queue persistence and deduplication."""

import pytest

from server import queue
from server.models import Job, Status


@pytest.fixture(autouse=True)
def isolated_queue(tmp_path, monkeypatch):
    path = tmp_path / "queue.json"
    monkeypatch.setattr(queue, "QUEUE_PATH", path)
    monkeypatch.setattr(queue, "LOCK_PATH", path.with_suffix(".lock"))


def test_add_and_read_back():
    job, created = queue.add(Job(url="https://example.com/a", title="A"))
    assert created
    assert [j.url for j in queue.all_jobs()] == ["https://example.com/a"]
    assert queue.get(job.id).title == "A"


def test_duplicate_url_is_not_queued_twice():
    queue.add(Job(url="https://example.com/a", title="A"))
    job, created = queue.add(Job(url="https://example.com/a", title="A again"))
    assert not created
    assert job.title == "A", "the original entry wins"
    assert len(queue.all_jobs()) == 1


def test_update_patches_one_job():
    job, _ = queue.add(Job(url="https://example.com/a"))
    queue.add(Job(url="https://example.com/b"))
    queue.update(job.id, status=Status.FILLED)
    assert queue.get(job.id).status == Status.FILLED
    assert len(queue.pending()) == 1


def test_pending_excludes_started_jobs():
    job, _ = queue.add(Job(url="https://example.com/a"))
    assert len(queue.pending()) == 1
    queue.update(job.id, status=Status.TAILORING)
    assert queue.pending() == []


def test_folder_name_is_stable_and_slugged():
    job = Job(url="https://example.com/a", title="Senior Backend Engineer (Remote)",
              company="Example Corp.")
    name = job.folder_name()
    assert "example-corp" in name
    assert "senior-backend-engineer-remote" in name
    assert name.endswith(job.id)
    assert job.folder_name(2).endswith("_v2")
