"""Launching work from the review page."""

import sys

import pytest

from server import runner


@pytest.fixture(autouse=True)
def no_live_fills(monkeypatch):
    monkeypatch.setattr(runner, "_fills", {})
    monkeypatch.setattr(runner, "_pgrep_fill", lambda job_id: None)


class FakePopen:
    def __init__(self, argv=None, **kwargs):
        self.argv = argv
        self.kwargs = kwargs
        self.pid = 4242
        self.returncode = None

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode


def test_launch_starts_the_script_detached(tmp_path, monkeypatch):
    seen = {}

    class FakePopen:
        def __init__(self, argv, **kwargs):
            seen["argv"] = argv
            seen["kwargs"] = kwargs
            self.pid = 4242

        def poll(self):
            return None

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
    monkeypatch.setattr(runner.subprocess, "Popen", FakePopen)
    assert runner.start_fill("c4f3", log_dir=tmp_path) == 4242


def test_launch_refuses_a_second_fill_for_the_same_job(tmp_path, monkeypatch):
    """Two apply.py runs on one job fight over the same Chrome tab."""
    monkeypatch.setattr(runner.subprocess, "Popen", FakePopen)
    assert runner.launch("apply.py", "c4f3", log_dir=tmp_path) == 4242
    assert runner.fill_pid("c4f3") == 4242
    assert runner.launch("apply.py", "c4f3", log_dir=tmp_path) is None
    # A different job is unaffected, and so is the pipeline.
    assert runner.launch("apply.py", "beef", log_dir=tmp_path) == 4242
    assert runner.launch("pipeline.py", "c4f3", log_dir=tmp_path) == 4242


def test_an_exited_fill_no_longer_counts(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.subprocess, "Popen", FakePopen)
    runner.launch("apply.py", "c4f3", log_dir=tmp_path)
    runner._fills["c4f3"][0].returncode = 0
    assert runner.fill_pid("c4f3") is None
    assert runner.fill_started_at("c4f3") is None


def test_stop_fill_kills_the_process_group(tmp_path, monkeypatch):
    """apply.py owns browser-use workers; Chrome is detached and must survive."""
    killed = []
    monkeypatch.setattr(runner.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(runner.os, "killpg", lambda pid, sig: killed.append((pid, sig)))
    runner.launch("apply.py", "c4f3", log_dir=tmp_path)

    assert runner.stop_fill("c4f3") == 4242
    assert killed == [(4242, runner.signal.SIGTERM)]
    assert runner.fill_pid("c4f3") is None
    assert runner.stop_fill("c4f3") is None
