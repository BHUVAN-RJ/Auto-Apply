"""Archive immutability and versioning."""

import csv

import pytest

from archive import store
from server.models import Job, Status


@pytest.fixture(autouse=True)
def isolated_archive(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "APPLICATIONS", tmp_path / "applications")
    monkeypatch.setattr(store, "INDEX_PATH", tmp_path / "applications" / "index.csv")


def make_job(url="https://example.com/jobs/1"):
    return Job(url=url, title="Backend Engineer", company="Example Corp")


def test_create_writes_job_json_and_index():
    app_dir = store.create(make_job())
    assert (app_dir / "job.json").exists()
    rows = list(csv.DictReader(store.INDEX_PATH.open()))
    assert rows[0]["folder"] == app_dir.name
    assert rows[0]["company"] == "Example Corp"


def test_second_run_gets_a_new_versioned_folder():
    job = make_job()
    first = store.create(job)
    second = store.create(job)
    assert first != second
    assert second.name.endswith("_v2")
    assert first.exists(), "the original folder must survive a re-run"


def test_write_refuses_to_overwrite():
    app_dir = store.create(make_job())
    store.write(app_dir, "resume.tex", "one")
    with pytest.raises(store.ArchiveError):
        store.write(app_dir, "resume.tex", "two")
    assert (app_dir / "resume.tex").read_text() == "one"


def test_status_history_accumulates():
    app_dir = store.create(make_job())
    store.set_status(app_dir, Status.TAILORING)
    store.set_status(app_dir, Status.AWAITING_REVIEW, "1 page")
    store.set_status(app_dir, Status.FILLED)

    assert store.read_status(app_dir) == "filled"
    import json
    history = json.loads((app_dir / "status.json").read_text())["history"]
    assert [h["status"] for h in history] == ["tailoring", "awaiting_review", "filled"]
    assert history[1]["note"] == "1 page"


def test_index_status_follows_the_folder():
    app_dir = store.create(make_job())
    store.set_status(app_dir, Status.FILLED)
    rows = list(csv.DictReader(store.INDEX_PATH.open()))
    assert rows[0]["status"] == "filled"
