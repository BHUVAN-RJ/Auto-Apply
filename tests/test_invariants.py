"""The rules this project exists to protect, checked without a browser or a model.

Run by `scripts/check.sh` after every install, every update and every change
anyone's Claude Code makes to their copy. A failure here means the change
broke a promise the app makes to the person using it; the change is undone,
not the test. CLAUDE.md ("The rule the whole project exists to protect")
and PLAN.md (Invariants) say why each one exists.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from browser import guard

ROOT = Path(__file__).resolve().parent.parent


# 1. The agent never submits an application.

@pytest.mark.parametrize("label", [
    "Submit", "Submit application", "Apply", "Apply now", "Send application", "Finish",
])
def test_a_submit_control_is_refused(label):
    assert guard.describes_submit(label)
    with pytest.raises(guard.SubmitBlocked):
        guard.check_click(text=label)


# 2. No visa, sponsorship or work-authorisation question is ever answered.

@pytest.mark.parametrize("label", [
    "Will you now or in the future require sponsorship for employment visa status?",
    "Are you legally authorized to work in the United States?",
    "Are you on OPT or STEM OPT?",
])
def test_a_visa_question_is_protected(label):
    assert guard.describes_protected(label)
    with pytest.raises(guard.ProtectedField):
        guard.check_protected(label=label)


def test_the_answering_model_refuses_a_visa_question():
    from tailor import answers
    with pytest.raises((answers.AnswerError, guard.ProtectedField)):
        answers.answer("Do you require visa sponsorship?", answers.Context(posting="x"))


def test_the_filler_never_fills_authorisation_from_the_profile():
    from browser.forms import profile
    assert not any(re.search(r"sponsor|authori[sz]|visa|opt\b", key, re.I) for key in profile.KEYS)


# 3. The browser is never killed and never owned by browser-use.

def test_nothing_kills_or_owns_the_browser():
    for path in list((ROOT / "browser").rglob("*.py")) + [ROOT / "apply.py"]:
        source = path.read_text()
        assert "browser.kill()" not in source, path
        assert "user_data_dir" not in source or path.name == "chrome.py", path


# 4. The data lives outside the code, so an update can never touch it.

def test_the_data_directory_is_not_the_clone(tmp_path):
    out = subprocess.run(
        [sys.executable, "-c",
         "import paths; print(paths.BASE); print(paths.DATA); print(paths.APPLICATIONS); print(paths.PROMPTS)"],
        cwd=ROOT, env={**os.environ, "AUTOPILOT_HOME": str(tmp_path)},
        capture_output=True, text=True, check=True,
    ).stdout.split()
    for line in out:
        assert Path(line).is_relative_to(tmp_path), line


# 5. A prompt edit cannot reach any of the above: every prompt is text, and
#    the rules are in code. The prompts never mention the guard's words as
#    permission.

def test_no_prompt_grants_submission():
    from tailor import prompts
    for prompt in prompts.CATALOG:
        text = prompt.stock().lower()
        assert "you may submit" not in text and "press submit" not in text.replace("never press submit", ""), prompt.name


# 6. The one place Submit is pressed is the person's own button, and the
#    agent cannot reach it. `browser/press_submit.py` exists because a human
#    asked for a Submit button on the review page; nothing about the agent
#    changes, and this is what keeps it that way.

def test_only_the_review_endpoint_presses_submit():
    presser = "press_submit"
    # Nothing the agent runs imports it: not the fill, not the browser stack,
    # not the script the approve button launches.
    for path in list((ROOT / "browser").rglob("*.py")) + [ROOT / "apply.py", ROOT / "pipeline.py"]:
        if path.name == "press_submit.py":
            continue
        assert presser not in path.read_text(), path
    # And it is reached from exactly one place in the server: the endpoint the
    # review page's button posts to.
    callers = [p for p in (ROOT / "server").rglob("*.py") if presser in p.read_text()]
    assert [p.name for p in callers] == ["review.py"], callers


def test_the_guard_still_refuses_the_control_the_button_presses():
    """The presser aims at `guard.SUBMIT_PATTERNS`; the agent is refused it.
    One list, two uses - so a pattern added for one is added for both."""
    import json

    from browser import press_submit

    for pattern in guard.SUBMIT_PATTERNS:
        assert json.dumps(pattern) in press_submit._SUBMIT_SOURCE
    assert press_submit._SUBMIT_SOURCE in press_submit.CANDIDATES_JS


def test_the_apply_press_can_never_be_a_submit():
    """Apply is pressed inside the fill, so the line between starting an
    application and sending one is checked here and not only in that
    module's own tests. Two halves: the label must read as a start and never
    as a finish, and the page must have nothing on it that could be sent."""
    from browser import open_apply, signin

    for label in ("Submit", "Submit Application", "Send", "Finish", "Complete application",
                  "Confirm and submit", "Withdraw"):
        assert open_apply.choose([{"text": label, "shown": True, "disabled": False, "top": 1}]) is None, label
    # A page holding an application is refused whatever its buttons say.
    assert not open_apply.safe_to_press(signin.Look(files=1))
    assert not open_apply.safe_to_press(signin.Look(fields=signin.FORM_FIELDS))
    assert not open_apply.safe_to_press(signin.Look(passwords=1))
    # And a control is only ever pressed through the one gated function.
    source = (ROOT / "browser" / "open_apply.py").read_text()
    assert source.count('await page.send("Input.dispatchMouseEvent"') == 1
    assert "safe_to_press" in source.split("async def press(")[1]
