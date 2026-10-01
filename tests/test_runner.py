"""Launching work from the review page."""

import subprocess
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
    monkeypatch.setattr(runner, "held", lambda: False)
    monkeypatch.setattr(runner, "any_fill_running", lambda: None)
    monkeypatch.setattr(runner.subprocess, "Popen", FakePopen)
    assert runner.start_fill("c4f3", log_dir=tmp_path) == 4242


def test_a_held_fill_never_opens_a_tab(tmp_path, monkeypatch):
    """The switch at the top of the page. The job is approved and its
    documents are written; what waits is the browser."""
    monkeypatch.setattr(runner, "AUTOFILL_ENABLED", True)
    monkeypatch.setattr(runner, "held", lambda: True)
    monkeypatch.setattr(runner.subprocess, "Popen",
                        lambda *a, **k: pytest.fail("a held fill opened a tab"))
    assert runner.start_fill("c4f3", log_dir=tmp_path) is None
    # A person pressing "Fill the form" on one job is not the bulk the hold
    # is for, so that one still goes.
    monkeypatch.setattr(runner.subprocess, "Popen", FakePopen)
    assert runner.start_fill("c4f3", log_dir=tmp_path, force=True) == 4242


def test_only_one_job_fills_at_a_time(tmp_path, monkeypatch):
    """Two fills share the one Chrome window the app owns, and open their
    tabs on top of each other. The second waits for the tick after the
    first ends."""
    monkeypatch.setattr(runner, "AUTOFILL_ENABLED", True)
    monkeypatch.setattr(runner, "held", lambda: False)
    monkeypatch.setattr(runner, "any_fill_running", lambda: "aaaa")
    monkeypatch.setattr(runner.subprocess, "Popen",
                        lambda *a, **k: pytest.fail("a second fill started"))
    assert runner.start_fill("bbbb", log_dir=tmp_path) is None


def test_the_line_starts_the_oldest_approved_job_and_only_one(tmp_path, monkeypatch):
    from server.models import Job, Status

    rows = [Job(url="https://x/1", title="A", company="C", status=Status.APPROVED, app_dir=str(tmp_path)),
            Job(url="https://x/2", title="B", company="C", status=Status.APPROVED, app_dir=str(tmp_path))]
    monkeypatch.setattr(runner, "AUTOFILL_ENABLED", True)
    monkeypatch.setattr(runner, "held", lambda: False)
    monkeypatch.setattr(runner, "any_fill_running", lambda: None)
    monkeypatch.setattr(runner.subprocess, "Popen", FakePopen)
    import server.queue as job_queue
    monkeypatch.setattr(job_queue, "all_jobs", lambda: rows)
    assert runner.serial_tick() == rows[0].id

    # Held, the line does not move at all.
    monkeypatch.setattr(runner, "held", lambda: True)
    assert runner.serial_tick() is None


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


@pytest.mark.real_fill_scan
def test_a_fill_started_by_another_process_is_seen(monkeypatch):
    """`_fills` is empty in a freshly started server, so the one-tab rule
    rests entirely on reading the process list. It was read with `pgrep -af`,
    which macOS does not support: bare pids came back, nothing matched, and
    the answer was always None - three fills on one Chrome window after a
    restart."""
    runner._fills.clear()
    lines = "54905 /usr/bin/python /repo/apply.py 3dcc\n"

    def fake_run(cmd, **kwargs):
        assert cmd[:2] == ["pgrep", "-fl"], "macOS pgrep has no -a"
        return subprocess.CompletedProcess(cmd, 0, stdout=lines, stderr="")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    assert runner.any_fill_running() == "3dcc"
    lines = "54905 /usr/bin/python /repo/apply.py\n"
    assert runner.any_fill_running() == "batch"
    # Output in a shape we did not expect is still a fill: saying "nothing is
    # running" is the one answer that opens a second tab.
    lines = "54905\n"
    assert runner.any_fill_running() == "unknown"
    lines = "\n"
    assert runner.any_fill_running() is None


def test_the_easy_ones_take_the_tab_first(monkeypatch):
    """Ashby and Greenhouse fill and land at checkpoint 2 on their own;
    Workday walks five pages and usually wants an account first. So a session
    finishes as many applications as it can before it meets one that needs a
    person. Within a family the queue's own order stands."""
    from server.models import Job, Status

    def job(name, url):
        # `Job.id` is a hash of the URL, not a field, so the title is what
        # this test reads the order off.
        return Job(url=url, title=name, source="x", status=Status.APPROVED,
                   app_dir=f"/tmp/{name}")

    rows = [
        job("w1", "https://acme.wd5.myworkdayjobs.com/careers/job/1"),
        job("other", "https://careers.example.com/jobs/9"),
        job("g1", "https://job-boards.greenhouse.io/embed/job_app?for=acme"),
        job("a1", "https://jobs.ashbyhq.com/acme/1"),
        job("a2", "https://jobs.ashbyhq.com/acme/2"),
        job("g2", "https://boards.greenhouse.io/acme/jobs/2"),
        job("l1", "https://jobs.lever.co/acme/1"),
    ]
    import server.queue as queue_module

    monkeypatch.setattr(queue_module, "all_jobs", lambda: list(rows))
    assert [j.title for j in runner.waiting()] == ["a1", "a2", "g1", "g2", "l1", "w1", "other"]


def test_the_running_order_is_read_off_the_host():
    assert runner.ats_rank("https://jobs.ashbyhq.com/acme/1") == 0
    assert runner.ats_rank("https://job-boards.greenhouse.io/embed/job_app?for=acme") == 1
    assert runner.ats_rank("https://jobs.lever.co/acme/1") == 2
    assert runner.ats_rank("https://acme.wd5.myworkdayjobs.com/x") == 3
    assert runner.ats_rank("https://careers.example.com/jobs/9") == 4
    assert runner.ats_rank("") == 4
