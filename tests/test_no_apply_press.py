"""The automatic path does not press Apply, on any system (2026-10-30).

Apply was pressed to get from a posting to the form behind it. Measured
cost: up to three presses per run - the posting, the system's own chooser,
"Apply Manually" - each of which can open a tab, which is where the stray
tabs came from; and 22 runs this month still ended "no application form on
this page". So the fill waits and hands the job back as it always did, and
the person presses Apply themselves and then **Start now**.

These are source-reading tests on purpose, and the house standard for that
is narrow: they are legitimate where the text *is* the promise. "Nothing the
automatic path runs asks for a press" is exactly that kind of promise - the
same shape as invariant 6's import graph - and it cannot be observed from a
test that stubs the browser, because a stub would be asserting the stub.
`browser/open_apply.py` keeps its own behavioural tests and its invariant;
it is simply no longer called from here.
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILL = ROOT / "browser" / "fill.py"


def _calls(tree: ast.AST) -> list[ast.Call]:
    return [node for node in ast.walk(tree) if isinstance(node, ast.Call)]


def test_the_fill_never_asks_for_an_apply_press():
    tree = ast.parse(FILL.read_text())
    asked = [kw for call in _calls(tree) for kw in call.keywords
             if kw.arg == "press_apply"]
    assert asked, "the sign-in watch is no longer told either way - check the call"
    for keyword in asked:
        assert isinstance(keyword.value, ast.Constant) and keyword.value.value is False, (
            "the fill asked for Apply to be pressed again"
        )


def test_the_fill_does_not_reach_the_apply_presser():
    source = FILL.read_text()
    code = "\n".join(line for line in source.splitlines()
                     if not line.lstrip().startswith("#"))
    assert "open_apply" not in code, "the fill names the Apply presser again"


def test_the_apply_presser_is_still_there_and_still_guarded():
    """Removed from the automatic path, not weakened: the review page's own
    flows and anything written later meet the same two halves."""
    from browser import open_apply, signin

    assert open_apply.choose([{"text": "Apply", "shown": True, "disabled": False, "top": 1}])
    for label in ("Submit", "Send", "Finish", "Sign in"):
        assert open_apply.choose([{"text": label, "shown": True, "disabled": False,
                                   "top": 1}]) is None, label
    assert not open_apply.safe_to_press(signin.Look(files=1))
    assert not open_apply.safe_to_press(signin.Look(passwords=1))
