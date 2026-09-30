"""Pressing Apply on a posting, and the one precondition that makes it safe.

Apply is not Submit, and the difference is checked on the page rather than
asserted: nothing is pressed unless the page has nowhere to attach a file and
too few fields to be an application. A page with an application on it is
refused whatever its buttons say.
"""

from __future__ import annotations

import asyncio

from browser import open_apply, signin


def look(**kw) -> signin.Look:
    base = {"url": "https://acme.wd5.myworkdayjobs.com/careers/job/1",
            "title": "Associate Software Engineer", "text": "", "fields": 0,
            "files": 0, "passwords": 0}
    base.update(kw)
    return signin.Look(**base)


def control(text, **kw):
    base = {"text": text, "shown": True, "disabled": False, "top": 100, "x": 50, "y": 20}
    base.update(kw)
    return base


# -- what may be pressed --------------------------------------------------

def test_a_posting_is_safe_to_press():
    assert open_apply.safe_to_press(look(fields=2))


def test_an_application_in_progress_is_never_pressed():
    """The precondition, and the whole reason this is not a submit: a page
    with somewhere to put a resume, or a form's worth of boxes, is an
    application that could be sent."""
    assert not open_apply.safe_to_press(look(files=1))
    assert not open_apply.safe_to_press(look(fields=signin.FORM_FIELDS))
    assert not open_apply.safe_to_press(look(passwords=1))


def test_the_labels_that_start_an_application():
    for text in ("Apply", "Apply Now", "Apply Manually", "Autofill with Resume",
                 "Use My Last Application", "Start Your Application", "Apply →"):
        assert open_apply.choose([control(text)]), text


def test_a_label_that_finishes_one_is_never_chosen():
    for text in ("Submit", "Submit Application", "Send application", "Finish",
                 "Withdraw application", "Save job", "Sign in", "Cancel"):
        assert open_apply.choose([control(text)]) is None, text


def test_prose_containing_apply_now_is_not_a_control():
    assert open_apply.choose([control("Apply now to join our team and help us build")]) is None


def test_a_hidden_or_disabled_control_is_not_pressed():
    assert open_apply.choose([control("Apply", shown=False)]) is None
    assert open_apply.choose([control("Apply", disabled=True)]) is None


def test_the_highest_apply_wins():
    """A posting's own Apply sits above the footer's boilerplate, and
    Workday's chooser lists its options in the order it wants them taken."""
    chosen = open_apply.choose([control("Apply Online", top=900), control("Apply", top=120)])
    assert chosen["text"] == "Apply"


# -- the press itself -----------------------------------------------------

class FakePage:
    def __init__(self, candidates):
        self.candidates = candidates
        self.sent = []

    async def evaluate(self, expression):
        if "elementFromPoint" in expression:
            return True
        return self.candidates

    async def send(self, method, params=None):
        self.sent.append((method, params))
        return {}


def test_the_press_is_a_real_mouse_event_at_the_control():
    page = FakePage([control("Apply", x=300, y=400)])
    label = asyncio.run(open_apply.press(page, look(fields=1)))
    assert label == "Apply"
    kinds = [params["type"] for method, params in page.sent if method == "Input.dispatchMouseEvent"]
    assert kinds == ["mousePressed", "mouseReleased"]
    assert all(params["x"] == 300 and params["y"] == 400
               for _, params in page.sent if _ == "Input.dispatchMouseEvent")


def test_nothing_is_pressed_on_a_page_with_a_form_on_it():
    page = FakePage([control("Apply")])
    assert asyncio.run(open_apply.press(page, look(files=1))) is None
    assert page.sent == []


def test_nothing_is_pressed_when_no_label_reads_as_apply():
    page = FakePage([control("Submit application")])
    assert asyncio.run(open_apply.press(page, look(fields=1))) is None
    assert page.sent == []


def test_the_submit_presser_is_not_reachable_from_here():
    """One list, one place: `press_submit` aims at `guard.SUBMIT_PATTERNS`
    and is reachable only from the review page's own button. The Apply press
    runs inside the fill, so it must not be able to get there."""
    import ast

    tree = ast.parse(open(open_apply.__file__).read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.update(f"{node.module or ''}.{a.name}" for a in node.names)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    assert not any("press_submit" in name or "guard" in name for name in imported), imported
    # And the patterns the agent is refused are not what this aims at.
    code = [line for line in open(open_apply.__file__).read().splitlines()
            if not line.lstrip().startswith("#")]
    assert not any("SUBMIT_PATTERNS" in line for line in code
                   if "`guard.SUBMIT_PATTERNS`" not in line)
