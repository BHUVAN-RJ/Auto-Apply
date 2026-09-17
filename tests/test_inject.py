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
    asyncio.run(go())


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


def test_post_refuses_paths_off_the_server():
    assert inject.post("http://evil.example/x", {}) == (False, 0, "bad path")


def test_no_chrome_is_launched_and_nothing_is_closed():
    # The injector only ever attaches; chrome.py owns the process and
    # server/review.py is the only closer.
    source = open(inject.__file__).read()
    assert "Browser.close" not in source
    assert "subprocess" not in source
    assert "kill" not in source
