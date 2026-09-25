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
    # Pointed at the job, never brought to the front: the fill's own tab
    # is what changes the active tab, on approve.
    assert "openReview(queuedId, { focus: false })" in script
    assert 'chrome.runtime.sendMessage({ type: "open-autopilot", url, focus })' in script
    assert "if (focus) window.open(url" in script
    background = (runner.ROOT / "capture" / "background.js").read_text()
    assert "chrome.tabs.create({ url, active: focus })" in background


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


def test_use_opus_marks_the_job_with_the_expensive_model(launched, monkeypatch):
    """The button is armed during the countdown and read when the job is
    queued. The row carries the resolved model name, not a flag, so the
    folder says what it was written with even if the setting moves."""
    from tailor import llm

    monkeypatch.setenv("OPENROUTER_PREMIUM_MODEL", "expensive/model")
    client = TestClient(app)
    body = client.post("/capture", json={"url": "https://example.com/jobs/9", "premium": True}).json()
    assert queue.get(body["id"]).tailor_model == "expensive/model"
    # Not pressed: nothing on the row, so the cheap default tailors it.
    body = client.post("/capture", json={"url": "https://example.com/jobs/10"}).json()
    assert queue.get(body["id"]).tailor_model is None
    assert llm.premium_model() == "expensive/model"


def test_use_opus_pressed_on_jobrights_page_reaches_the_employer_tabs_capture(launched, monkeypatch):
    """Jobright's Apply opens the employer tab, and that tab is what queues
    the job, so the choice travels by posting id like auto-approve does."""
    monkeypatch.setenv("OPENROUTER_PREMIUM_MODEL", "expensive/model")
    client = TestClient(app)
    assert client.post("/prefs", json={"jr_id": "6aad9999", "premium": True}).json() == {"ok": True}
    body = client.post("/capture", json={"url": "https://boards.example.com/apply?jr_id=6aad9999"}).json()
    assert queue.get(body["id"]).tailor_model == "expensive/model"
    from server import app as app_module
    assert "6aad9999" not in app_module.PREMIUM_PREFS, "used once; a later capture is not bound by it"


def test_the_countdown_leaves_room_to_press_use_opus():
    """Both the button and the three seconds are in the banner; two seconds
    was not enough to read the verdict and decide."""
    content = (runner.ROOT / "capture" / "content.js").read_text()
    assert "const AUTO_ADD_MS = 3000;" in content
    assert 'data-act="opus"' in content
    # Armed, not pressed: the countdown is not cancelled by the choice.
    assert "usePremium = !usePremium;" in content
    assert "addToQueue(autoFill(), usePremium)" in content


def test_a_job_already_applied_for_shouts_and_the_bar_stays_open():
    """Re-applying to a job autopilot already submitted is the mistake the
    seen check exists to stop, and a line inside a bar that folds into a
    badge after five seconds was missed. An applied job gets the loudest
    block the banner has, and that bar does not collapse."""
    script = (runner.ROOT / "capture" / "content.js").read_text()
    assert 'const APPLIED_STATUS = new Set(["submitted", "filled", "filling"])' in script
    assert "function appliedNotice(seen)" in script
    assert "ALREADY APPLIED" in script
    assert "You already applied to this job with Autopilot" in script
    assert ".applied { display: block;" in script
    # It stays open: the badge is how the warning was missed.
    assert 'if (!pending && (seen || verdict === "submitted") && !applied) {' in script
    # And it is in the bar's body, above the ordinary seen line.
    assert "${applied}\n          ${seen ?" in script
