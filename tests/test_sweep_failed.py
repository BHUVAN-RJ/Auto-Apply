"""Moving failed application folders aside, without losing the queue's link."""

import json

import pytest

from archive import store
from server import queue
from server.models import Job, Status
from tools import sweep_failed


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    qpath = tmp_path / "queue.json"
    monkeypatch.setattr(queue, "QUEUE_PATH", qpath)
    monkeypatch.setattr(queue, "LOCK_PATH", qpath.with_suffix(".lock"))
    monkeypatch.setattr(store, "APPLICATIONS", tmp_path / "applications")
    monkeypatch.setattr(store, "INDEX_PATH", tmp_path / "applications" / "index.csv")


def folder(url: str, status: Status):
    job, _ = queue.add(Job(url=url, title="Role", company="Co"))
    app_dir = store.create(job)
    store.set_status(app_dir, status)
    queue.update(job.id, status=status, app_dir=str(app_dir))
    return job, app_dir


def test_only_failed_folders_move(monkeypatch):
    failed_job, failed_dir = folder("https://example.com/1", Status.FAILED)
    _, good_dir = folder("https://example.com/2", Status.SUBMITTED)

    moved = sweep_failed.sweep()

    assert [m[0] for m in moved] == [failed_dir]
    assert not failed_dir.exists()
    assert (store.APPLICATIONS / "failed" / failed_dir.name / "status.json").exists()
    assert good_dir.exists()


def test_the_queue_row_follows_the_folder():
    job, failed_dir = folder("https://example.com/1", Status.FAILED)
    sweep_failed.sweep()
    assert queue.get(job.id).app_dir == str(store.APPLICATIONS / "failed" / failed_dir.name)


def test_a_row_pointing_at_a_later_run_is_left_alone():
    """A job that failed once and then succeeded points at the good folder."""
    job, failed_dir = folder("https://example.com/1", Status.FAILED)
    good_dir = store.create(job)
    store.set_status(good_dir, Status.SUBMITTED)
    queue.update(job.id, status=Status.SUBMITTED, app_dir=str(good_dir))

    sweep_failed.sweep()

    assert not failed_dir.exists()
    assert queue.get(job.id).app_dir == str(good_dir)


def test_dry_run_moves_nothing():
    _, failed_dir = folder("https://example.com/1", Status.FAILED)
    moved = sweep_failed.sweep(dry_run=True)
    assert len(moved) == 1
    assert failed_dir.exists()


def test_a_second_sweep_skips_the_failed_directory_itself():
    folder("https://example.com/1", Status.FAILED)
    sweep_failed.sweep()
    assert sweep_failed.sweep() == []
