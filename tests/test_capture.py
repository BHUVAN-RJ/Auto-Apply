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


def test_screen_banner_minimizes_to_a_restorable_badge():
    script = (runner.ROOT / "capture" / "content.js").read_text()
    assert 'data-act="expand"' in script
    assert 'class="assistant-logo"' in script
    assert 'class="assistant-ring"' in script
    assert 'class="assistant-core"' in script
    assert 'class="shell${bannerCollapsed ? " collapsed" : ""}"' in script
    assert 'shell.classList.add("collapsed")' in script
    assert 'shell.classList.remove("collapsed")' in script
    assert 'host.dataset.collapsed = "true"' in script
    assert 'collapseTimer = setTimeout' in script
    assert 'action === "expand"' in script
    assert 'host.addEventListener("click"' in script


def test_failed_screen_still_offers_add_to_autopilot():
    script = (runner.ROOT / "capture" / "content.js").read_text()
    assert 'const canRetry = !pending && (verdict === "error" || verdict === "not_a_job")' in script
    assert '${canRetry ? `<button data-act="again">Again</button>` : ""}' in script
    assert '${canAdd ? `<button class="add"' in script


def test_add_opens_the_captured_job_in_autopilot():
    script = (runner.ROOT / "capture" / "content.js").read_text()
    assert 'return { label: data.created ? "Added" : "Already in autopilot", id: data.id || null, created: !!data.created }' in script
    assert "openReview(queuedId)" in script
    assert 'chrome.runtime.sendMessage({ type: "open-autopilot", url })' in script


def test_a_newly_queued_employer_tab_closes_itself_and_nothing_else_does():
    """The tab Apply opened is screened and queued, then goes; approve
    opens a fresh one. Never a Jobright tab, never one already in
    autopilot (a fill may be on it)."""
    script = (runner.ROOT / "capture" / "content.js").read_text()
    assert "if (!onJobright && result?.created) closeSelf();" in script
    assert 'post("/__close", {})' in script
    assert 'chrome.runtime.sendMessage({ type: "close-me" })' in script
    background = (runner.ROOT / "capture" / "background.js").read_text()
    assert 'message?.type === "close-me"' in background
    assert "chrome.tabs.remove(tabId)" in background


def test_the_banners_auto_approve_box_travels_with_the_capture(launched):
    """Unchecked during the countdown: the job waits at checkpoint 1 for
    the human whatever the global switch says. Checked or absent: the
    global switch decides (None on the row)."""
    client = TestClient(app)
    body = client.post("/capture", json={"url": "https://example.com/jobs/9", "auto_fill": False}).json()
    assert queue.get(body["id"]).auto_fill is False
    body = client.post("/capture", json={"url": "https://example.com/jobs/10", "auto_fill": True}).json()
    assert queue.get(body["id"]).auto_fill is True
    body = client.post("/capture", json={"url": "https://example.com/jobs/11"}).json()
    assert queue.get(body["id"]).auto_fill is None


def test_a_choice_made_on_jobrights_page_reaches_the_employer_tabs_capture(launched):
    """On Jobright's posting page the employer tab does the queueing, so the
    box's answer goes ahead by posting id and is picked up once."""
    client = TestClient(app)
    assert client.post("/prefs", json={"jr_id": "6aad1234", "auto_fill": False}).json() == {"ok": True}
    body = client.post("/capture", json={"url": "https://boards.example.com/apply?jr_id=6aad1234"}).json()
    assert queue.get(body["id"]).auto_fill is False
    from server import app as app_module
    assert "6aad1234" not in app_module.AUTO_FILL_PREFS, "used once; a later capture is not bound by it"
