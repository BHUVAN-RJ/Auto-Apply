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
