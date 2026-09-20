"""Pressing Jobright's Autofill in code: finds the button, clicks once,
waits for the fields, and steps aside for the agent on anything else."""
import asyncio

import pytest

from browser import autofill


class FakePage:
    """Answers the three scripts from a scripted page state."""

    def __init__(self, button=None, fills=(), finished_after=None, signals=(), panels=()):
        self.button = button
        self.fills = list(fills)      # filled-field counts, one per status poll
        self.finished_after = finished_after
        self.signals = list(signals)  # Jobright's message state, one per poll
        self.panels = list(panels)    # Jobright's panel reading, one per poll
        self.listening = False
        self.clicks = 0
        self.polls = 0

    async def send(self, method, params=None):
        self.sent = getattr(self, "sent", []) + [(method, params)]
        return {}

    async def evaluate(self, expression):
        if expression == "document.readyState":
            return "complete"
        if expression == autofill.LISTEN_JS:
            self.listening = True
            return True
        if expression == autofill.FIND_JS:
            return self.button
        if expression == autofill.CLICK_JS:
            self.clicks += 1
            return self.button is not None
        if expression == autofill.STATUS_JS:
            self.polls += 1
            filled = self.fills[min(self.polls - 1, len(self.fills) - 1)] if self.fills else 0
            finished = self.finished_after is not None and self.polls > self.finished_after
            signal = self.signals[min(self.polls - 1, len(self.signals) - 1)] if self.signals else None
            panel = self.panels[min(self.polls - 1, len(self.panels) - 1)] if self.panels else None
            return {"filled": filled, "finished": finished, "signal": signal, "panel": panel}
        raise AssertionError(expression)


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(autofill, "POLL", 0.0)
    monkeypatch.setattr(autofill, "BUTTON_TIMEOUT", 0.05)
    monkeypatch.setattr(autofill, "FINISH_TIMEOUT", 0.5)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(autofill.asyncio, "sleep", lambda s: real_sleep(0))


def test_presses_once_and_waits_for_the_panel_to_finish():
    page = FakePage(button={"text": "Autofill", "tag": "button"}, fills=[2, 2, 9, 14, 14], finished_after=3)
    result = asyncio.run(autofill.run(page, "ABCDEF1234"))
    assert result.clicked and result.finished and page.clicks == 1
    assert result.filled_before == 2 and result.filled_after == 14
    assert result.tab_id == "1234"
    assert "pressed by code" in result.summary()


def test_fields_holding_still_count_as_finished():
    page = FakePage(button={"text": "Autofill with Jobright", "tag": "div"}, fills=[0, 5, 9, 9, 9, 9])
    result = asyncio.run(autofill.run(page))
    assert result.clicked and not result.finished and result.filled_after == 9
    assert "fields stopped changing" in result.summary()


def test_no_button_means_the_agent_does_it():
    page = FakePage(button=None)
    result = asyncio.run(autofill.run(page))
    assert not result.clicked and page.clicks == 0
    assert "not pressed by code" in result.summary()


def test_a_button_that_reads_as_submit_is_refused():
    page = FakePage(button={"text": "Autofill and submit", "tag": "button"}, fills=[1])
    result = asyncio.run(autofill.run(page))
    assert not result.clicked and page.clicks == 0
    assert "refused" in result.note


def test_the_finder_never_matches_submit_words():
    # The page-side regex anchors on "autofill"; a submit control cannot match it.
    import re
    pattern = re.compile(r"^\s*(auto-?fill)\b", re.I)
    assert pattern.match("Autofill") and pattern.match(" auto-fill form")
    for text in ("Submit application", "Apply now", "Autofill" + "x" * 50, "Use autofill"):
        assert not (pattern.match(text) and len(text) < 40), text


def test_jobrights_own_message_ends_the_wait_and_names_the_empty_fields():
    """Jobright posts its progress to window.top; the listener installed
    before the click catches the final result, so the wait ends the moment
    the extension says so, with its list of what it left empty."""
    working = {"events": 3, "done": False, "current": "Phone", "filled": ["Name"], "missing": []}
    final = {"events": 4, "done": True, "current": None, "filled": ["Name", "Phone"], "missing": ["Visa status", "Why us?"]}
    page = FakePage(button={"text": "Autofill", "tag": "button"}, fills=[1, 3, 5, 5, 5, 5, 5],
                    signals=[working, working, final])
    result = asyncio.run(autofill.run(page))
    assert page.listening and result.clicked and result.finished and result.signalled
    assert result.missing == ["Visa status", "Why us?"]
    assert page.polls == 3, "stopped on the message, not on the field count settling"
    assert "Jobright reported done" in result.summary() and "Why us?" in result.summary()


def test_a_snapshot_with_nothing_in_progress_counts_as_done():
    page = FakePage(button={"text": "Autofill", "tag": "button"}, fills=[1, 4, 6, 6, 6, 6, 6],
                    signals=[{"events": 1, "done": False, "current": "Name", "filled": [], "missing": []},
                             {"events": 5, "done": False, "current": None, "filled": ["Name"], "missing": []}])
    result = asyncio.run(autofill.run(page))
    assert result.signalled and page.polls == 2


BUSY = {"busy": True, "done": False, "filled": None, "total": None}
IDLE = {"busy": False, "done": True, "filled": 9, "total": 10}
BLANK = {"busy": False, "done": False, "filled": None, "total": None}


def test_the_panel_going_from_autofilling_to_a_count_ends_the_wait():
    # Greenhouse: react-select values never show in the field count and
    # the message may never come; the panel's own words are the signal.
    page = FakePage(button={"text": "Autofill", "tag": "button"}, fills=[0],
                    panels=[BLANK, BUSY, BUSY, BUSY, IDLE, IDLE])
    result = asyncio.run(autofill.run(page))
    assert result.clicked and result.finished and not result.signalled
    assert result.panel == "9/10" and page.polls == 5
    assert "panel says 9/10 filled" in result.summary()


def test_a_count_that_sits_untouched_counts_as_done():
    # Jobright filled the tab before we pressed; nothing ever starts.
    page = FakePage(button={"text": "Autofill", "tag": "button"}, fills=[0], panels=[IDLE])
    result = asyncio.run(autofill.run(page))
    assert result.finished and result.panel == "9/10" and page.polls == autofill.SETTLE_POLLS + 1  # one before the press


def test_a_still_field_count_does_not_end_the_wait_while_the_panel_is_busy():
    page = FakePage(button={"text": "Autofill", "tag": "button"}, fills=[0, 5, 5, 5, 5, 5, 5, 5],
                    panels=[BUSY, BUSY, BUSY, BUSY, BUSY, BUSY, IDLE])
    result = asyncio.run(autofill.run(page))
    assert result.finished and result.panel == "9/10" and page.polls == 7


def test_a_busy_panel_holds_off_the_second_press():
    page = FakePage(button={"text": "Autofill my application", "tag": "div"}, fills=[0],
                    panels=[BUSY, BUSY, IDLE])
    calls = []
    original = page.evaluate

    async def evaluate(expression):
        if expression == autofill.FIND_JS and page.clicks:
            calls.append(1)
            return {"text": "Autofill", "tag": "button"}
        return await original(expression)
    page.evaluate = evaluate
    result = asyncio.run(autofill.run(page))
    assert result.finished and page.clicks == 1 and not calls


def test_a_tab_whose_panel_shows_a_count_is_reused():
    result = autofill.from_status({"filled": 5, "signal": {"events": 0}, "panel": IDLE}, "ABCDEF1234")
    assert result.clicked and result.finished and not result.signalled and result.panel == "9/10"


def test_a_tab_still_autofilling_is_not_reused():
    status = {"filled": 5, "signal": {"events": 5, "done": False, "current": "Name", "filled": [], "missing": []},
              "panel": BUSY}
    assert autofill.from_status(status, "T1") is None


def test_a_press_already_made_in_the_tab_is_reused():
    # The injector's listener left Jobright's messages on the window.
    status = {"filled": 14, "finished": False,
              "signal": {"events": 5, "done": True, "current": None, "filled": ["Name"], "missing": ["Phone"]}}
    result = autofill.from_status(status, "T1")
    assert result.clicked and result.signalled and result.finished
    assert result.filled_after == 14 and result.missing == ["Phone"]
    assert "pressed when the tab opened" in result.summary() or result.note


def test_a_tab_without_a_press_is_not_reused():
    assert autofill.from_status({"filled": 3, "signal": None}, "T1") is None
    assert autofill.from_status({"filled": 3, "signal": {"events": 0}}, "T1") is None


def test_the_signal_script_is_a_no_op_without_the_banner():
    js = autofill.signal_js("working", 'say "hi"')
    assert js.startswith("window.__autopilotAutomation && ")
    assert '"working"' in js and '\\"hi\\"' in js


def test_a_second_differently_worded_control_gets_one_press():
    # "Autofill my application" opened the panel; "Autofill" inside it fills.
    page = FakePage(button={"text": "Autofill my application", "tag": "button"}, fills=[0, 0, 0, 5, 9, 9, 9])
    finds = iter([{"text": "Autofill", "tag": "span"}])
    original = page.evaluate

    async def evaluate(expression):
        if expression == autofill.FIND_JS and page.clicks:
            return next(finds, {"text": "Autofill", "tag": "span"})
        return await original(expression)
    page.evaluate = evaluate
    result = asyncio.run(autofill.run(page))
    assert result.clicked and page.clicks == 2 and result.filled_after == 9


def test_the_file_chooser_is_intercepted_only_during_the_press():
    page = FakePage(button={"text": "Autofill", "tag": "button"}, fills=[0, 4, 4, 4, 4])
    asyncio.run(autofill.run(page))
    flags = [p["enabled"] for m, p in page.sent if m == "Page.setInterceptFileChooserDialog"]
    assert flags == [True, False]



def test_same_page_tells_greenhouse_embeds_apart_and_ignores_visitor_tags():
    # One path for every job on the embedded board: matching on the path
    # put one job's resume on another job's form, three times.
    pallet = "https://job-boards.greenhouse.io/embed/job_app?for=pallet&token=1&jr_id=abc"
    amira = "https://job-boards.greenhouse.io/embed/job_app?for=amira&token=2&jr_id=abc"
    assert not autofill.same_page(pallet, amira)
    assert autofill.same_page(pallet, "https://job-boards.greenhouse.io/embed/job_app?token=1&for=pallet&jr_id=zzz&utm_source=x#top")
    assert autofill.same_page("https://jobs.ashbyhq.com/acme/123/application", "https://jobs.ashbyhq.com/acme/123/application/?jr_id=1")
    assert not autofill.same_page("", "")


def test_find_tab_matches_the_form_not_the_path(monkeypatch):
    import io
    pages = [{"type": "page", "id": "AMIRA", "url": "https://job-boards.greenhouse.io/embed/job_app?for=amira&token=2"},
             {"type": "page", "id": "PALLET", "url": "https://job-boards.greenhouse.io/embed/job_app?for=pallet&token=1&jr_id=q"}]
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=5: io.BytesIO(__import__("json").dumps(pages).encode()))
    assert autofill.find_tab("http://x", "https://job-boards.greenhouse.io/embed/job_app?for=pallet&token=1") == "PALLET"
    assert autofill.find_tab("http://x", "https://job-boards.greenhouse.io/embed/job_app?for=other&token=3") == ""
