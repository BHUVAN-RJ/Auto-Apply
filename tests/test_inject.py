"""The CDP injector: which tabs get the capture script, and how they talk back.

No browser: events are fed to the handler and the CDP calls it makes are
recorded. The script is a marker string; what it does is content.js's own
business.
"""

import asyncio
import json

import pytest

from browser import inject

JOBRIGHT = "https://jobright.ai/jobs/info/abc123"
EMPLOYER = "https://jobs.example.com/apply/1?jr_id=abc123"
ELSEWHERE = "https://news.example.com/article"


class Recorder(inject.Injector):
    def __init__(self):
        super().__init__("ws://unused", "SCRIPT")
        self.calls = []

    async def send(self, method, params=None, session=None):
        self.calls.append((method, params or {}, session))
        return {}


@pytest.fixture(autouse=True)
def fake_autofill(monkeypatch):
    """The injector presses Autofill in loaded employer tabs; here the press
    is recorded, not made, and reports a click."""
    presses = []

    async def run(page, target_id=""):
        presses.append(target_id)
        return inject.autofill.AutofillResult(target_id=target_id, clicked=True, signalled=True,
                                              filled_before=0, filled_after=9)
    monkeypatch.setattr(inject.autofill, "run", run)
    # The press then tells the server; never the real one from a test.
    monkeypatch.setattr(inject, "post", lambda path, body: (True, 200, {"action": "none", "id": None}))
    return presses


def created(target_id, url, type_="page", opener=None):
    info = {"targetId": target_id, "type": type_, "url": url}
    if opener:
        info["openerId"] = opener
    return {"method": "Target.targetCreated", "params": {"targetInfo": info}}


def attached(target_id, url, session, type_="page"):
    return {"method": "Target.attachedToTarget",
            "params": {"sessionId": session, "targetInfo": {"targetId": target_id, "type": type_, "url": url}}}


def changed(target_id, url):
    return {"method": "Target.targetInfoChanged",
            "params": {"targetInfo": {"targetId": target_id, "type": "page", "url": url}}}


def loaded(session):
    return {"method": "Page.loadEventFired", "params": {}, "sessionId": session}


def run(injector, *events):
    async def go():
        for event in events:
            await injector.on_event(event)
        # The press runs as a task off the event loop, and its report to
        # the server in a thread; let both finish.
        for _ in range(20):
            await asyncio.sleep(0.005)
    asyncio.run(go())


def signalled(injector, session):
    return [params["expression"] for method, params, s in injector.calls
            if method == "Runtime.evaluate" and s == session and "__autopilotAutomation" in params.get("expression", "")]


def injected(injector):
    return [session for method, params, session in injector.calls
            if method == "Runtime.evaluate" and params.get("expression") == "SCRIPT"]


def test_source_hosts_and_tags():
    assert inject.from_source(JOBRIGHT)
    assert inject.from_source("https://app.jobright.ai/x")
    assert not inject.from_source("https://notjobright.ai/x")
    assert not inject.from_source("https://jobright.ai.example.com/x")
    assert inject.tagged(EMPLOYER)
    assert not inject.tagged("https://jobs.example.com/jr_id=1")  # path, not query


def test_a_jobright_tab_gets_the_script_on_attach_and_on_load():
    r = Recorder()
    run(r, created("t1", JOBRIGHT), attached("t1", JOBRIGHT, "s1"), loaded("s1"))
    assert injected(r) == ["s1", "s1"]
    assert ("Runtime.addBinding", {"name": inject.BINDING}, "s1") in r.calls


def test_an_unrelated_tab_gets_nothing():
    r = Recorder()
    run(r, created("t1", ELSEWHERE), attached("t1", ELSEWHERE, "s1"), loaded("s1"))
    assert injected(r) == []


def test_a_tab_opened_from_jobright_is_followed():
    r = Recorder()
    run(r, created("t1", JOBRIGHT), created("t2", ELSEWHERE, opener="t1"),
        attached("t2", ELSEWHERE, "s2"), loaded("s2"))
    assert injected(r) == ["s2", "s2"]


def test_jobrights_tagged_apply_tab_is_followed_without_an_opener():
    # Jobright's extension creates the tab itself: no openerId, no windowOpen.
    r = Recorder()
    run(r, created("t2", EMPLOYER), attached("t2", EMPLOYER, "s2"), loaded("s2"))
    assert injected(r) == ["s2", "s2"]


def test_navigating_off_jobright_in_the_same_tab_is_followed():
    r = Recorder()
    run(r, created("t1", JOBRIGHT), attached("t1", JOBRIGHT, "s1"),
        changed("t1", ELSEWHERE), loaded("s1"))
    assert injected(r)[-1] == "s1"
    assert "t1" in r.marked


def test_only_page_targets_are_touched():
    r = Recorder()
    run(r, created("w1", JOBRIGHT, type_="service_worker"),
        attached("w1", JOBRIGHT, "s1", type_="service_worker"))
    assert r.calls == []


def test_the_bridge_answers_the_page_through_the_server(monkeypatch):
    r = Recorder()
    monkeypatch.setattr(inject, "post", lambda path, body: (True, 200, {"created": True, "path": path}))
    run(r, created("t1", JOBRIGHT), attached("t1", JOBRIGHT, "s1"),
        {"method": "Runtime.bindingCalled", "sessionId": "s1",
         "params": {"name": inject.BINDING, "payload": json.dumps({"id": 7, "path": "/capture", "body": {}})}})
    reply = [p["expression"] for m, p, s in r.calls if m == "Runtime.evaluate" and s == "s1"][-1]
    assert reply.startswith("window.__autopilotReply(7, true, 200, ")
    assert '"path": "/capture"' in reply


def test_the_bridge_reports_a_down_server_as_not_ok(monkeypatch):
    r = Recorder()
    monkeypatch.setattr(inject, "post", lambda path, body: (False, 0, "Is the server running? refused"))
    run(r, created("t1", JOBRIGHT), attached("t1", JOBRIGHT, "s1"),
        {"method": "Runtime.bindingCalled", "sessionId": "s1",
         "params": {"name": inject.BINDING, "payload": json.dumps({"id": 1, "path": "/screen", "body": {}})}})
    reply = [p["expression"] for m, p, s in r.calls if m == "Runtime.evaluate" and s == "s1"][-1]
    assert reply.startswith("window.__autopilotReply(1, false, 0, ")


def test_open_review_reuses_focuses_and_navigates_the_autopilot_tab():
    r = Recorder()
    r.urls = {"job": EMPLOYER, "dashboard": f"{inject.SERVER_ORIGIN}/#old"}
    r.sessions = {"job-session": "job", "dashboard-session": "dashboard"}

    async def go():
        return await r.open_tab(f"{inject.SERVER_ORIGIN}/#new")

    ok, status, body = asyncio.run(go())

    assert ok and status == 200 and body["reused"] is True
    assert ("Page.navigate", {"url": f"{inject.SERVER_ORIGIN}/#new"}, "dashboard-session") in r.calls
    assert ("Target.activateTarget", {"targetId": "dashboard"}, None) in r.calls
    assert not any(method == "Target.createTarget" for method, _, _ in r.calls)


def test_open_review_creates_an_autopilot_tab_when_none_exists():
    r = Recorder()
    r.urls = {"job": EMPLOYER}

    async def go():
        return await r.open_tab(f"{inject.SERVER_ORIGIN}/#new")

    ok, status, body = asyncio.run(go())

    assert ok and status == 200 and body["reused"] is False
    assert ("Target.createTarget", {"url": f"{inject.SERVER_ORIGIN}/#new", "background": False}, None) in r.calls


def test_open_review_without_focus_leaves_the_active_tab_alone():
    """A job added from the posting points the review tab at it but
    never takes the person off the page; the fill's own tab does that,
    on approve."""
    r = Recorder()
    r.urls = {"job": EMPLOYER, "dashboard": f"{inject.SERVER_ORIGIN}/#old"}
    r.sessions = {"job-session": "job", "dashboard-session": "dashboard"}
    ok, status, body = asyncio.run(r.open_tab(f"{inject.SERVER_ORIGIN}/#new", focus=False))
    assert ok and body["reused"] is True
    assert ("Page.navigate", {"url": f"{inject.SERVER_ORIGIN}/#new"}, "dashboard-session") in r.calls
    assert not any(method == "Target.activateTarget" for method, _, _ in r.calls)

    r = Recorder()
    r.urls = {"job": EMPLOYER}
    ok, status, body = asyncio.run(r.open_tab(f"{inject.SERVER_ORIGIN}/#new", focus=False))
    assert body["reused"] is False
    assert ("Target.createTarget", {"url": f"{inject.SERVER_ORIGIN}/#new", "background": True}, None) in r.calls


def test_open_review_refuses_a_lookalike_origin():
    r = Recorder()

    async def go():
        return await r.open_tab(f"{inject.SERVER_ORIGIN}.evil.example/#job")

    ok, status, _body = asyncio.run(go())

    assert ok and status == 403
    assert r.calls == []


def test_post_refuses_paths_off_the_server(monkeypatch):
    monkeypatch.undo()  # the real `post`, not the fixture's stub
    assert inject.post("http://evil.example/x", {}) == (False, 0, "bad path")


def test_no_chrome_is_launched_and_the_browser_is_never_closed():
    # The injector only ever attaches; chrome.py owns the process and
    # server/review.py is the only closer of the browser. The one tab it
    # closes is the queued employer tab, at that page's own request.
    source = open(inject.__file__).read()
    assert "Browser.close" not in source
    assert "subprocess" not in source
    assert "kill" not in source


def test_autofill_is_pressed_once_when_an_employer_tab_loads(fake_autofill):
    r = Recorder()
    r.autofill_on_open = True
    run(r, created("t2", EMPLOYER), attached("t2", EMPLOYER, "s2"), loaded("s2"), loaded("s2"))
    assert fake_autofill == ["t2"]
    states = signalled(r, "s2")
    assert '"working"' in states[0] and '"done"' in states[-1]


def test_autofill_done_is_reported_and_the_agent_taking_over_is_shown(fake_autofill, monkeypatch):
    """Autofill over is the trigger for the browser agent on a job already
    in autopilot; the server says whether it started one."""
    posted = []
    monkeypatch.setattr(inject, "post", lambda path, body: posted.append((path, body)) or (True, 200, {"action": "fill", "id": "ab12"}))
    r = Recorder()
    r.autofill_on_open = True
    run(r, created("t2", EMPLOYER), attached("t2", EMPLOYER, "s2"), loaded("s2"))
    assert posted and posted[0][0] == "/autofilled" and posted[0][1]["url"] == EMPLOYER
    assert "pressed by code" in posted[0][1]["note"]
    assert '"working"' in signalled(r, "s2")[-1] and "taking over" in signalled(r, "s2")[-1]


def test_autofill_is_not_pressed_on_jobright_or_unrelated_tabs(fake_autofill):
    r = Recorder()
    r.autofill_on_open = True
    run(r, created("t1", JOBRIGHT), attached("t1", JOBRIGHT, "s1"), loaded("s1"),
        created("t3", ELSEWHERE), attached("t3", ELSEWHERE, "s3"), loaded("s3"))
    assert fake_autofill == []


def test_autofill_on_open_is_off_by_default(fake_autofill, monkeypatch):
    """The employer tab is screened and queued, then goes; the fill presses
    Autofill in its own tab on approve."""
    monkeypatch.delenv(inject.AUTOFILL_ON_OPEN, raising=False)
    r = Recorder()
    run(r, created("t2", EMPLOYER), attached("t2", EMPLOYER, "s2"), loaded("s2"))
    assert fake_autofill == []
    monkeypatch.setenv(inject.AUTOFILL_ON_OPEN, "1")
    assert Recorder().autofill_on_open


def bridge(path, body=None, id_=1):
    return {"method": "Runtime.bindingCalled", "sessionId": None,
            "params": {"name": inject.BINDING, "payload": json.dumps({"id": id_, "path": path, "body": body or {}})}}


def closes(injector):
    return [params["targetId"] for method, params, _ in injector.calls if method == "Target.closeTarget"]


def test_a_queued_employer_tab_may_close_itself():
    r = Recorder()
    event = bridge("/__close"); event["sessionId"] = "s2"
    run(r, created("t2", EMPLOYER), attached("t2", EMPLOYER, "s2"), event)
    assert closes(r) == ["t2"]
    # The page hears back before the tab goes.
    order = [m for m, _, _ in r.calls if m in ("Runtime.evaluate", "Target.closeTarget")]
    assert order.index("Target.closeTarget") > order.index("Runtime.evaluate")


def test_only_an_employer_tab_from_jobright_may_close(monkeypatch):
    monkeypatch.setattr(inject, "post", lambda path, body: (True, 200, {}))
    r = Recorder()
    review = f"{inject.SERVER_ORIGIN}/#ab12"
    events = []
    for target, url, session in (("t1", JOBRIGHT, "s1"), ("t3", ELSEWHERE, "s3"), ("t4", review, "s4")):
        event = bridge("/__close"); event["sessionId"] = session
        events += [created(target, url), attached(target, url, session), event]
    run(r, *events)
    assert closes(r) == []
    replies = [params["expression"] for method, params, _ in r.calls
               if method == "Runtime.evaluate" and "__autopilotReply" in params.get("expression", "")]
    assert len(replies) == 3 and all("403" in reply for reply in replies)

