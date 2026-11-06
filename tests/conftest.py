import os
import shutil
import tempfile
from pathlib import Path

# Before anything imports `paths`: the suite gets a data folder of its own,
# with the template as the master resume. Tests used to read the clone's
# base/resume.tex and applicant.md, which exist only on the maintainer's machine, so a
# fresh clone failed fourteen of them; and none should ever see a real
# person's queue, settings or stories.
_HOME = Path(tempfile.mkdtemp(prefix="autopilot-test-home-"))
(_HOME / "base").mkdir()
_BASE = Path(__file__).resolve().parent.parent / "base"
shutil.copy(_BASE / "resume.template.tex", _HOME / "base" / "resume.tex")
shutil.copy(_BASE / "applicant.example.md", _HOME / "base" / "applicant.md")
os.environ["AUTOPILOT_HOME"] = str(_HOME)

import pytest  # noqa: E402

from tailor import prompts, workshop  # noqa: E402


@pytest.fixture(autouse=True)
def _own_prompt_edits(tmp_path, monkeypatch):
    """No test reads the person's own prompt edits or Workshop history:
    both would change what a model stub is sent, on one machine only."""
    monkeypatch.setattr(prompts, "OVERLAY", tmp_path / "_prompts")
    monkeypatch.setattr(workshop, "STORE", tmp_path / "_workshop.json")
    # A test that re-tailors a few times must not start a real model call.
    monkeypatch.setattr(workshop, "LEARN_EVERY", 10**6)


@pytest.fixture(autouse=True)
def _no_real_fills(request, monkeypatch):
    """The process list belongs to the machine, not to the suite. The
    one-tab-at-a-time guard reads it with pgrep, so a real `apply.py` running
    on the maintainer's laptop made `/fill` answer 409 in five tests that
    have nothing to do with it. The test that means to exercise the guard
    itself says so with `@pytest.mark.real_fill_scan`.
    """
    from server import runner

    if request.node.get_closest_marker("real_fill_scan"):
        return
    monkeypatch.setattr(runner, "any_fill_running", lambda: None)
    monkeypatch.setattr(runner, "_pgrep_fill", lambda job_id: None)


@pytest.fixture(autouse=True)
def _no_real_gmail_or_linkedin(monkeypatch):
    """No test drives the person's own Gmail or LinkedIn.

    Both bridges shell out to tools on this machine that act in a real,
    signed-in account: one writes a draft into their mailbox, the other sends
    a connection request to a stranger. They are switched off for every test,
    and a test that wants the path stubs the bridge itself.
    """
    from outreach import gmailthing, linkedinthing

    monkeypatch.setenv(gmailthing.ENV, "0")
    monkeypatch.setenv(linkedinthing.ENV, "0")


@pytest.fixture(autouse=True)
def _no_real_web_research(monkeypatch):
    """No test reaches the Claude Code CLI or the public web.

    The outreach path asks the subscription to find a company's recruiters
    before it asks Apollo, and that call is a real subprocess doing real web
    searches. The suite stubs every model; this is one, and a test that wants
    it stubs `outreach.people` itself.
    """
    from outreach import people

    monkeypatch.setenv(people.ENV, "0")
