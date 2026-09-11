"""Launching work from the review page."""

import sys

import pytest

from server import runner


def test_launch_starts_the_script_detached(tmp_path, monkeypatch):
    seen = {}

    class FakePopen:
        def __init__(self, argv, **kwargs):
            seen["argv"] = argv
            seen["kwargs"] = kwargs
            self.pid = 4242

    monkeypatch.setattr(runner.subprocess, "Popen", FakePopen)
    pid = runner.launch("apply.py", "c4f3", log_dir=tmp_path)

    assert pid == 4242
    assert seen["argv"][0] == sys.executable, "the venv interpreter must be inherited"
    assert seen["argv"][1].endswith("apply.py")
    assert seen["argv"][2] == "c4f3"
    assert seen["kwargs"]["start_new_session"] is True, (
        "a fill must survive the server stopping, or it leaves a half-filled form")
    assert (tmp_path / "apply_c4f3.log").exists()


def test_launch_returns_none_for_a_missing_script(tmp_path):
    assert runner.launch("nope.py", "c4f3", log_dir=tmp_path) is None


def test_start_fill_does_nothing_unless_autofill_is_switched_on(tmp_path, monkeypatch):
    """The default is manual, so approval cannot launch a browser by surprise."""
    monkeypatch.setattr(runner, "AUTOFILL_ENABLED", False)
    monkeypatch.setattr(runner.subprocess, "Popen",
                        lambda *a, **k: pytest.fail("must not launch when off"))
    assert runner.start_fill("c4f3", log_dir=tmp_path) is None


def test_start_fill_launches_when_switched_on(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "AUTOFILL_ENABLED", True)

    class FakePopen:
        def __init__(self, *a, **k):
            self.pid = 5
    monkeypatch.setattr(runner.subprocess, "Popen", FakePopen)
    assert runner.start_fill("c4f3", log_dir=tmp_path) == 5
