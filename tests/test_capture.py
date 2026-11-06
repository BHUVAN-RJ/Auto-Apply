"""Capturing a job starts the pipeline; nothing past checkpoint 1 starts."""

import pathlib
import re

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


def test_arming_opus_is_a_choice_about_the_job_and_not_an_instruction_to_add_it():
    """Pressing "Use Opus" records which model will tailor this one job; it
    adds nothing by itself, and the add that follows reads the choice.

    This test used also to pin the auto-add countdown at two seconds. That
    countdown was withdrawn on 2026-10-30 at the maintainer's request - it
    had queued `login.usc.edu/login` twice, a SAP page titled "Career
    Opportunities: Sign In" and a bare careers homepage, two of them marked
    submitted - so there is no duration left to assert. What it must not do
    is come back, and that is
    `test_nothing_on_the_posting_page_acts_by_itself` below.
    """
    content = (runner.ROOT / "capture" / "content.js").read_text()
    assert 'data-act="opus"' in content
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
    # answer yet - which is now only a screen still running.
    # One exception now, not two: the countdown that was the other one is
    # gone (2026-10-30), so this asserts that no second exception exists.
    assert "if (!pending) {" in script
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
    """A click can be a mistake, so the button that added the job is the one
    that takes it back out, and the server side is the review page's own
    reject - the folder and the record stay. (It was built because a
    countdown ran out while someone was still reading; the countdown went on
    2026-10-30 and Undo stays, because the reason is now the click itself.)

    The assertion on "click to stop" was dropped with the countdown: it had
    stopped matching anything but a CSS comment quoting the old label.
    """
    script = (runner.ROOT / "capture" / "content.js").read_text()
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


def test_the_put_chip_tags_the_input_and_asks_for_the_logo():
    """The chip sets the form's own file input; the tag is what the logo
    hangs off, written before the events so a form that re-renders on change
    still has it on the input that was set (2026-10-04, asked for)."""
    content = (runner.ROOT / "capture" / "content.js").read_text()
    put = content.split("function putFile(")[1].split("\nfunction ")[0]
    assert 'setAttribute("data-autopilot-ref"' in put
    assert put.index("data-autopilot-ref") < put.index('new Event("change"')
    chip = content.split('for (const button of row.querySelectorAll(".chip.put"))')[1][:2000]
    assert "/mark`" in chip and "key" in chip
    # Only after the file is actually on the slot.
    assert "if (!result.ok) return;" in chip


# That `putFile` never puts the resume in the system's own "upload a resume
# and we will fill the form in" slot is tested by running `putFile` against
# the live Ashby form's own markup: `tests/test_dom.py`. A test here read
# the function's source for the filter expression; it was deleted on
# 2026-10-06, because a check that reads source cannot tell a rewrite from a
# regression, and the one above it (which reads what the chip *does* with
# the input it chose) is the shape the rest of this file keeps.


def test_the_jobright_apply_press_waits_and_is_a_real_press():
    """Clicking Jobright's Apply was broken three ways: the first element in
    document order whose text began with "apply" (a hidden control, or
    "Apply filters"), a scripted `el.click()` React ignores on a primary
    control, and a press made the instant the countdown ended, before
    Jobright's own panel had mounted - which is where the stray blank tabs
    came from."""
    script = (runner.ROOT / "capture" / "content.js").read_text()
    # The label is the whole label of a visible, enabled control.
    assert "const APPLY_LABEL = /^" in script
    assert "const NOT_APPLY = /filter|" in script
    assert "if (!APPLY_LABEL.test(label) || NOT_APPLY.test(label)) continue;" in script
    assert 'if (el.disabled || el.getAttribute("aria-disabled") === "true") continue;' in script
    assert "if (rect.width < 2 || rect.height < 2) continue;" in script
    # A real press, not el.click().
    assert 'el.dispatchEvent(new PointerEvent("pointerdown", at));' in script
    assert 'el.dispatchEvent(new MouseEvent("click", { ...at, buttons: 0 }));' in script
    assert "button.click();" not in script
    # Waited for, then settled, then pressed.
    assert "const APPLY_WAIT_MS = 6000;" in script
    assert "const APPLY_SETTLE_MS = 1500;" in script
    assert "while (!button && performance.now() < until) {" in script
    assert "await new Promise((r) => setTimeout(r, APPLY_SETTLE_MS));" in script
    assert "if (await openJob()) {" in script


def test_nothing_on_the_posting_page_acts_by_itself():
    """The one promise the whole 2026-10-30 change is: the banner screens,
    the banner says, and the banner waits.

    This reads the source, which this file is otherwise sparing with - but
    here the **absence** is the promise, the way invariant 6 promises that
    the agent cannot import the submit presser. A countdown reintroduced
    under any name is the defect coming back, and the only honest check for
    "there is no timer that queues a job" is that there is no timer that
    queues a job.

    What it queued while it existed, off the rows it left behind:
    `login.usc.edu/login` twice, a SAP SuccessFactors page whose title is
    "Career Opportunities: Sign In", a bare `careers.vituity.com/`
    homepage - two of those marked submitted - and a row whose entire title
    is `Apply`. A marked tab stays marked through every navigation, so a
    single-sign-on redirect inside one was screened and queued as the job.
    """
    script = (runner.ROOT / "capture" / "content.js").read_text()
    for gone in ("AUTO_ADD_MS", "AUTO_ADD ", 'classList.add("counting")',
                 'add.classList.remove("counting")'):
        assert gone not in script, f"the auto-add countdown is back: {gone!r}"

    # `act()` queues the job and `openJob()` presses Jobright's Apply.
    # Neither may be reached from a timer, an interval or a frame callback:
    # the only call site of each is a listener on a control.
    for name in ("act(", "openJob()"):
        for call in re.finditer(re.escape(name), script):
            line = script[script.rfind("\n", 0, call.start()) + 1:
                          script.index("\n", call.start())]
            assert "setTimeout" not in line and "setInterval" not in line \
                and "requestAnimationFrame" not in line, \
                f"{name} is reached from a timer: {line.strip()!r}"


def test_the_add_button_says_what_pressing_it_does_on_each_kind_of_page():
    """Jobright's own posting page cannot be queued (the server refuses a
    jobright.ai URL), so there Add means "press their Apply, and add it from
    the page that opens" - and the button says so instead of claiming the
    job went in. Everywhere else it is the capture itself."""
    script = (runner.ROOT / "capture" / "content.js").read_text()
    assert '"Opened — adding it there…"' in script
    assert 'label("Adding…")' in script
    # The Jobright label no longer instructs a second press, because
    # `/asked` carries the first one across to the employer tab. Asserted as
    # an absence as well, so the old wording cannot drift back in beside the
    # new one (guardian's condition, 2026-10-31).
    assert "press Add on the job page" not in script


def test_the_add_press_crosses_the_two_tabs_and_is_spent_once():
    """One press on the posting is the whole decision.

    `/capture` refuses a `jobright.ai` URL, so the only queueable URL is the
    employer's, and the only way there is Jobright's own Apply - a second
    tab. Without a channel between them, removing the countdown turned one
    press into two. `/asked` is that channel and it carries one fact: the
    person asked for this posting. It is spent on the first read, so one
    press can never queue two jobs.
    """
    from server import app as app_module

    app_module.ASKED_PREFS.clear()
    client = TestClient(app)
    assert client.post("/asked", json={"jr_id": "abc123"}).json()["asked"] is False

    client.post("/prefs", json={"jr_id": "abc123", "asked": True, "auto_fill": False})
    assert client.post("/asked", json={"jr_id": "abc123"}).json()["asked"] is True
    # Spent: the employer tab that already queued it cannot queue it again,
    # and neither can the next tab to carry the same tag.
    assert client.post("/asked", json={"jr_id": "abc123"}).json()["asked"] is False
    # The decisions it travelled with are still the posting's own.
    assert app_module.AUTO_FILL_PREFS.get("abc123") is False


def test_a_press_does_not_keep():
    """A decision expires. Somebody who pressed Add an hour ago and has
    since wandered onto another employer's page in a tab that still carries
    the tag has not asked for *that* job.

    The clock is moved by ageing the stored press, not by patching
    `time.monotonic`: that patch reaches the whole interpreter, including
    the HTTP client's own waiting, and the request then never returns (it
    hung this file's run for 400 s before it was found).
    """
    import time as real_time

    from server import app as app_module

    app_module.ASKED_PREFS.clear()
    client = TestClient(app)
    client.post("/prefs", json={"jr_id": "stale", "asked": True})
    app_module.ASKED_PREFS["stale"] = real_time.monotonic() - app_module.ASKED_TTL - 1
    assert client.post("/asked", json={"jr_id": "stale"}).json()["asked"] is False
    # Expired, and taken off the shelf rather than left to be reconsidered.
    assert "stale" not in app_module.ASKED_PREFS

    # A press inside the window still counts, so the test above is about the
    # age and not about the key.
    client.post("/prefs", json={"jr_id": "fresh", "asked": True})
    assert client.post("/asked", json={"jr_id": "fresh"}).json()["asked"] is True


def test_nothing_is_asked_for_by_default():
    """The flag is only ever written by a press: `/prefs` carrying the
    per-job decisions and no `asked` must not arm anything."""
    from server import app as app_module

    app_module.ASKED_PREFS.clear()
    client = TestClient(app)
    client.post("/prefs", json={"jr_id": "quiet", "auto_fill": True, "premium": True})
    assert app_module.ASKED_PREFS == {}
    assert client.post("/asked", json={"jr_id": "quiet"}).json()["asked"] is False


def test_an_honoured_press_is_spent_only_on_a_page_that_reads_as_a_job():
    """`canAdd` is not the gate, and the guardian was right to insist.

    The Add *button* is offered on a reject, a failed screen and a
    `not_a_job` page on purpose: a person overruling a verdict with it in
    front of them is a decision they are allowed to make. Honouring a press
    made on the posting is not that - a `jr_id` tag survives a redirect, so
    a marked tab that follows single sign-on to a university or an
    employer's login still carries it, and that is precisely the three bad
    captures the countdown made (a USC SSO page, a SuccessFactors sign-in,
    a bare careers homepage). A press behind them would not make them good.
    """
    script = (runner.ROOT / "capture" / "content.js").read_text()
    assert 'const QUEUEABLE = new Set(["ok", "caution"]);' in script
    # The call site is gated on the verdict, not only on `canAdd`.
    assert "canAdd && QUEUEABLE.has(verdict)" in script


def test_an_honoured_add_needs_a_tag_a_job_verdict_and_no_wall():
    """What authorises queueing a page nobody clicked on.

    It used to be a press: Add on Jobright wrote a flag the employer tab
    spent. The maintainer retired that on 2026-10-31 - "As soon as we open
    from Jobright, I want it to be auto-applied... else I have to manually
    add every job, which is again pointless" - so the authorisation is now
    the `jr_id` tag, which only Jobright's extension puts on, and `/asked`
    decides the wording and nothing else.

    The rule underneath did not move: this must never queue a wall or a
    page that is not a posting. Three gates carry it now, and this is the
    test of all three.
    """
    script = (runner.ROOT / "capture" / "content.js").read_text()
    body = script.split("async function honourTheAdd(", 1)[1].split("\n}\n", 1)[0]

    # 1. The tag is the authorisation.
    assert "if (!id) return;" in body
    # 2. A wall is refused, and refused *before* the round trip: a page
    #    asking for a password must not depend on the server answering.
    assert "if (looksLikeSignIn()) return;" in body
    assert body.index("looksLikeSignIn()") < body.index("try {")
    # 3. The verdict gate at the call site, which is the one that did not
    #    move - a failed screen, an auto-reject and a `not_a_job` page keep
    #    their Add button and may not be queued without a click on it.
    call = script.split("honourTheAdd(label, act)", 1)[0].rsplit("if (", 1)[1]
    assert "QUEUEABLE.has(verdict)" in call
    assert 'const QUEUEABLE = new Set(["ok", "caution"]);' in script
    # 4. The press is still the last thing in the function, so the catch
    #    falling through to it reads as deliberate rather than as a hole.
    assert body.rstrip().endswith("await act();")


def test_the_pages_list_of_identity_hosts_covers_the_servers():
    """One list, two languages, and the server's is where a host gets added.

    `browser/signin.py` is the full version of this question and is what
    the fill consults; the page has a small copy so it can refuse to queue
    a wall without asking anybody. A host known to the server and missing
    from the page would be refused by the fill and queued by the banner,
    which is the worst of both. The page may hold more (university single
    sign-on is ours), never fewer.
    """
    from browser import signin

    script = (runner.ROOT / "capture" / "content.js").read_text()
    listed = script.split("const IDENTITY_HOSTS = [", 1)[1].split("];", 1)[0]
    page_hosts = set(re.findall(r'"([^"]+)"', listed))
    missing = set(signin.IDENTITY_HOSTS) - page_hosts
    assert not missing, f"known to the server, invisible to the page: {sorted(missing)}"


def test_the_cold_button_on_every_page_says_add_rather_than_screen():
    """A button on any page, and its label is the person's intent.

    2026-11-03, asked for: "When I open a page from outside, I want a start
    button or something that starts the process... I need to click 'Add this
    to job queue' or something. Let's show that button on every page." The
    orb on its own offered "click to screen this posting", which is the
    machine's first step and not what the press is for, and the real
    decision sat behind a second press on a banner nobody had asked to read.
    """
    script = (runner.ROOT / "capture" / "content.js").read_text()
    cold = script.split("function renderCold()", 1)[1].split("\n}\n", 1)[0]
    assert ">Add to autopilot</span>" in cold
    assert "Screen this page with Job Autopilot" not in script
    # The press is a press, not a timer, and it arms the add rather than
    # queueing from here: the banner's own `act()` is the one implementation.
    assert "coldAdd = true;" in cold
    assert "addToQueue(" not in cold


def test_a_cold_press_adds_the_job_without_a_second_press():
    """One press for both halves: the screen the pipeline needs, then the
    queue. The banner's own Add is what runs, so auto-approve, Opus and Undo
    all keep working exactly as they do when the button is pressed by hand.
    """
    script = (runner.ROOT / "capture" / "content.js").read_text()
    honour = script.split("if (coldAdd && canAdd", 1)[1].split("\n  }\n", 1)[0]
    # Spent before it is honoured, so a re-render of the same banner (the
    # job picker, the thread, a file list landing) cannot add twice.
    assert honour.index("coldAdd = false;") < honour.index("act()")
    # A wall is not a posting, however it was reached - the same rule the
    # Jobright path carries, and the two captures the old countdown made.
    assert "looksLikeSignIn()" in honour
    # Already in autopilot: no Add to press, so nothing to honour.
    assert "canAdd && !seen && !onJobright" in script


def test_no_markup_is_written_through_a_raw_html_sink():
    """A Trusted Types page must still get the button.

    Google's own careers pages send `require-trusted-types-for 'script'`,
    and under it `innerHTML =` throws - so the badge never rendered there at
    all, which on a job board is the whole feature missing. `DOMParser` is
    not a way round it: it is the same sink and throws the same way
    (measured live on that page before this was written). The policy is,
    and the markup it blesses is written in this file and never anything the
    page said.
    """
    script = (runner.ROOT / "capture" / "content.js").read_text()
    assert 'tt.createPolicy("job-autopilot"' in script
    # One sink, inside the helper. Anything else is a page that renders
    # nothing under a policy nobody notices until a job cannot be added.
    sinks = [line.strip() for line in script.splitlines()
             if ".innerHTML = " in line or "parseFromString(" in line]
    assert sinks == ["root.innerHTML = trustedHTML(html);"], sinks
    # A site whose own directive names its policies refuses to make ours;
    # the markup still goes on, because most pages have no policy at all.
    assert "htmlPolicy = null;" in script
