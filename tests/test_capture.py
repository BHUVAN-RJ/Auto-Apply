"""Capturing a job starts the pipeline; nothing past checkpoint 1 starts."""

import pathlib

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
    autopilot (a fill may be on it), and never one the person asked to
    screen from its badge - there the posting is the page they are on."""
    script = (runner.ROOT / "capture" / "content.js").read_text()
    assert "if (!onJobright && result?.created && !askedByHand) closeSelf();" in script
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
    from server import settings as settings_module
    from tailor import byhand, llm

    monkeypatch.setenv("OPENROUTER_PREMIUM_MODEL", "expensive/model")
    client = TestClient(app)

    # The default: Opus is asked for by hand, so the row carries the handoff
    # and the pipeline stops with a prompt to copy rather than buying the
    # most expensive call in the app.
    body = client.post("/capture", json={"url": "https://example.com/jobs/8", "premium": True}).json()
    assert queue.get(body["id"]).tailor_model == byhand.BY_HAND

    # With that switched off it is the API model, as it always was.
    monkeypatch.setattr(settings_module, "opus_by_hand", lambda: False)
    body = client.post("/capture", json={"url": "https://example.com/jobs/9", "premium": True}).json()
    assert queue.get(body["id"]).tailor_model == "expensive/model"
    # Not pressed: nothing on the row, so the cheap default tailors it.
    body = client.post("/capture", json={"url": "https://example.com/jobs/10"}).json()
    assert queue.get(body["id"]).tailor_model is None
    assert llm.premium_model() == "expensive/model"


def test_use_opus_pressed_on_jobrights_page_reaches_the_employer_tabs_capture(launched, monkeypatch):
    """Jobright's Apply opens the employer tab, and that tab is what queues
    the job, so the choice travels by posting id like auto-approve does."""
    from server import settings as settings_module
    monkeypatch.setattr(settings_module, "opus_by_hand", lambda: False)
    monkeypatch.setenv("OPENROUTER_PREMIUM_MODEL", "expensive/model")
    client = TestClient(app)
    assert client.post("/prefs", json={"jr_id": "6aad9999", "premium": True}).json() == {"ok": True}
    body = client.post("/capture", json={"url": "https://boards.example.com/apply?jr_id=6aad9999"}).json()
    assert queue.get(body["id"]).tailor_model == "expensive/model"
    from server import app as app_module
    assert "6aad9999" not in app_module.PREMIUM_PREFS, "used once; a later capture is not bound by it"


def test_the_countdown_is_two_seconds_and_opus_does_not_cancel_it():
    """Two seconds (2026-10-03, asked for; three from 2026-09-24 to give the
    Opus button room). Arming Opus never cancelled the countdown, and the
    thread's own "Re-tailor with Opus" catches a press made after the job
    has gone in, so the extra second bought nothing."""
    content = (runner.ROOT / "capture" / "content.js").read_text()
    assert "const AUTO_ADD_MS = 2000;" in content
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
    assert "Applied" in script and "waiting for your Submit" in script
    # And the heading is true of the status it is over: only a submitted job
    # has been applied for. A filled one is the tailored form waiting in a
    # tab for the person's own Submit, and shouting ALREADY APPLIED over it
    # reads as the block pointing at the wrong job.
    assert '"ALREADY APPLIED"' in script
    assert '"ALREADY IN AUTOPILOT"' in script and '"FILLING THIS NOW"' in script
    assert ".applied.pending" in script
    # One row, not a panel: heading and one line of detail side by side.
    # It used to stack what, when, the company, "Same posting." and a link,
    # which took a third of the bar to say what fits on a line.
    assert ".applied { display: flex;" in script
    # And it is the way back to what was sent: the block itself is the
    # button, with the promise in its tooltip. The bar's own open button is
    # suppressed while it shows, because the same sentence twice reads as
    # two different offers.
    assert 'data-act="open" role="button"' in script
    assert "Show me what I sent: the posting" in script
    assert 'title="${esc(promise)}"' in script
    assert "${seen && !applied ? `<button class=\"open\"" in script
    assert "function appliedWhen(at)" in script


def test_a_decided_job_is_described_by_its_outcome_not_by_where_it_is_kept():
    """"Already in autopilot · Rejected" led with the plumbing. A posting
    opened a second time is answered with APPLIED, REJECTED (and why), or
    the failure, and that line is the way back to it."""
    script = (runner.ROOT / "capture" / "content.js").read_text()
    assert 'submitted: "APPLIED"' in script
    assert 'skipped: "REJECTED"' in script
    assert "seen.status === \"skipped\" && seen.reject" in script
    assert "function openLabel(seen)" in script
    assert '"Show me why I rejected it"' in script
    # Said once: the loud block or the line, never both.
    assert "${seen && !applied ?" in script
    # Every banner folds into the badge once it has a verdict, including
    # this one: the block is the loudest thing the banner draws and the
    # badge keeps its colour, so holding it open was holding one bar open
    # for good. The two that do not fold are the two that are not an
    # answer yet - a screen still running, and a countdown about to queue.
    assert 'if (!pending && !(add && add.classList.contains("counting"))) {' in script
    assert "const AUTO_COLLAPSE_MS = 3000;" in script
    # Opening the badge is a decision to keep the bar (2026-09-30, reversing
    # the rule above for this one case): a bar that opened itself folds
    # itself away, a bar the person opened stays until they close it.
    assert "const expand = () => {" in script and "    stopCollapse();\n  };" in script
    assert 'shell.addEventListener("mouseenter", stopCollapse);' in script
    assert 'shell.addEventListener("mouseleave", leave);' in script
    # And it is in the bar's body, in place of the ordinary seen line: the
    # same fact twice, once loudly and once quietly, reads as two facts.
    assert "${applied}\n          ${seen && !applied ?" in script


def test_the_submission_watch_only_silences_a_confirmation_page():
    """It was meant to stop the banner painting a verdict over the page a
    submitted form turns into. Restored from sessionStorage, it silenced
    the tab for the rest of its life: a Workday tab that had held a fill
    was signed into and opened on the real application form, and no banner
    came back - no verdict, and no way to put the tailored resume on the
    slot."""
    script = (runner.ROOT / "capture" / "content.js").read_text()
    assert "function notTheConfirmation()" in script
    assert "if (submissionWatched && !force && !notTheConfirmation()) return;" in script
    assert "if (submissionWatched && !notTheConfirmation()) return;" in script
    # The job id and the poll stay: a confirmation reached later is still
    # marked, and `/screen` answers `submitted` for one before any rule runs.
    assert "sessionStorage.removeItem(JOB_KEY)" in script


def test_the_banner_can_take_a_job_back_out():
    """Auto-add is a countdown, and a countdown runs out while someone is
    still reading. The button that added the job is the one that undoes it,
    and the server side is the review page's own reject, so the folder and
    the record stay."""
    script = (runner.ROOT / "capture" / "content.js").read_text()
    assert "click to stop" in script, "the countdown says it can be stopped"
    assert 'data-act = "undo"' in script.replace(".dataset.act = ", "data-act = ")
    assert '"changed_my_mind"' in script
    assert "/reject" in script


def test_a_bar_the_person_opened_stays_open():
    """The three-second fold is for a bar that put itself on the screen. One
    the person opened by tapping the badge is open because they want
    something from it - the files, the reasons, the way back to the job -
    and none of that is done in three seconds."""
    script = (runner.ROOT / "capture" / "content.js").read_text()
    expand = script.split("const expand = () => {")[1].split("};")[0]
    assert "openedByHand = true" in expand and "stopCollapse()" in expand
    assert "autoCollapse()" not in expand
    # And moving the mouse off it does not start the countdown either.
    leave = script.split("const leave = () =>")[1].split(";")[0]
    assert "!openedByHand" in leave


def test_the_file_chips_speak_for_themselves():
    """A line explaining drag, click and put took more of the bar than the
    files it explained."""
    script = (runner.ROOT / "capture" / "content.js").read_text()
    assert "Drag onto a file slot, click to download" not in script.split("title=")[0]
    assert 'class="chip put"' in script


def test_i_submitted_it_is_not_asked_twice():
    """Pressing the button is the statement. It records what the person did,
    writes no correction and closes nothing but that form's tab, so a dialog
    on top of it is a second click for nothing (2026-10-03, asked for). The
    dialog that stays is the one on "Submit it", which presses Submit."""
    page = (pathlib.Path(__file__).resolve().parent.parent / "review" / "index.html").read_text()
    marked = page.split("async function markSubmitted(")[1].split("\n}")[0]
    assert "confirm(" not in marked
    assert "confirm(" in page.split("async function submitNow(")[1].split("\n}")[0]
