"""The Chrome launcher: reuse a listening browser, launch detached otherwise."""

from pathlib import Path

import pytest

from browser import chrome


def test_reuses_a_chrome_that_is_already_listening(tmp_path, monkeypatch):
    monkeypatch.setattr(chrome, "is_listening", lambda url, timeout=1.0: True)

    def must_not_launch(*args, **kwargs):
        raise AssertionError("launch() must not run when a browser is listening")

    monkeypatch.setattr(chrome, "launch", must_not_launch)
    assert chrome.ensure(tmp_path, 9333) == "http://127.0.0.1:9333"


def test_launches_when_nothing_is_listening(tmp_path, monkeypatch):
    monkeypatch.setattr(chrome, "is_listening", lambda url, timeout=1.0: False)
    calls = []

    def fake_launch(directory, port_number, headless=False):
        calls.append((directory, port_number, headless))
        return chrome.cdp_url(port_number)

    monkeypatch.setattr(chrome, "launch", fake_launch)
    assert chrome.ensure(tmp_path, 9444) == "http://127.0.0.1:9444"
    assert calls == [(tmp_path, 9444, False)]


def test_port_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv(chrome.PORT_ENV, "9555")
    assert chrome.port() == 9555
    monkeypatch.delenv(chrome.PORT_ENV)
    assert chrome.port() == chrome.DEFAULT_PORT


def test_launch_detaches_chrome_from_this_process(tmp_path, monkeypatch):
    """Chrome must outlive apply.py, and must not sit in the terminal's process group."""
    monkeypatch.setattr(chrome, "find_browser", lambda: "/fake/chrome")
    seen = {}

    class FakePopen:
        def __init__(self, args, **kwargs):
            seen["args"] = args
            seen.update(kwargs)

    monkeypatch.setattr(chrome.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(chrome, "is_listening", lambda url, timeout=1.0: True)

    url = chrome.launch(tmp_path / "profile", 9333)
    assert url == "http://127.0.0.1:9333"
    assert seen["start_new_session"] is True
    assert seen["args"][0] == "/fake/chrome"
    assert "--remote-debugging-port=9333" in seen["args"]
    assert f"--user-data-dir={tmp_path / 'profile'}" in seen["args"]
    assert "--headless=new" not in seen["args"]
    assert (tmp_path / "profile").is_dir(), "the directory must exist before launch"


def test_launch_reports_a_browser_that_never_opens_its_port(tmp_path, monkeypatch):
    monkeypatch.setattr(chrome, "find_browser", lambda: "/fake/chrome")
    monkeypatch.setattr(chrome.subprocess, "Popen", lambda *a, **k: None)
    monkeypatch.setattr(chrome, "is_listening", lambda url, timeout=1.0: False)
    monkeypatch.setattr(chrome, "LAUNCH_TIMEOUT", 0.0)
    with pytest.raises(chrome.ChromeError, match="9333"):
        chrome.launch(tmp_path, 9333)


def test_launch_fails_plainly_without_a_browser(tmp_path, monkeypatch):
    monkeypatch.setattr(chrome, "find_browser", lambda: None)
    with pytest.raises(chrome.ChromeError, match=chrome.EXECUTABLE_ENV):
        chrome.launch(tmp_path, 9333)


def test_is_listening_is_false_for_a_closed_port():
    assert chrome.is_listening("http://127.0.0.1:1", timeout=0.2) is False


def test_the_launcher_never_kills_what_it_starts():
    import inspect

    source = inspect.getsource(chrome)
    for forbidden in (".kill(", ".terminate(", "os.kill"):
        assert forbidden not in source, f"{forbidden} has no place in the launcher"


def test_close_is_a_no_op_when_nothing_is_listening(monkeypatch):
    monkeypatch.setattr(chrome, "is_listening", lambda url, timeout=1.0: False)
    monkeypatch.setattr(chrome.urllib.request, "urlopen",
                        lambda *a, **k: pytest.fail("must not talk to a browser"))
    assert chrome.close(9333) is False


def test_close_sends_browser_close_over_cdp(monkeypatch):
    import io
    import json as json_module
    import sys
    import types

    listening = [True]
    monkeypatch.setattr(chrome, "is_listening", lambda url, timeout=1.0: listening[0])

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(chrome.urllib.request, "urlopen", lambda url, timeout=2: Response(
        json_module.dumps({"webSocketDebuggerUrl": "ws://127.0.0.1:9333/devtools/browser/x"}).encode()))

    sent = []

    class Socket:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def send(self, message):
            sent.append(json_module.loads(message))
            listening[0] = False

        def recv(self, timeout=None):
            return "{}"

    fake = types.ModuleType("websockets.sync.client")
    fake.connect = lambda url, **k: Socket()
    monkeypatch.setitem(sys.modules, "websockets.sync.client", fake)

    assert chrome.close(9333) is True
    assert sent == [{"id": 1, "method": "Browser.close"}]
