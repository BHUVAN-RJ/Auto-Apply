"""Workday, page by page, without a browser.

The driver is deliberately dull - look, decide, do one thing, look again -
so the whole of it can be tested against a scripted tenant: a posting, an
account page, five form pages and the review. What matters here is where it
stops. It never presses Submit, it stops on anything only the person can do,
and it puts the resume on the one page that asks for it.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

from browser import workday


def page(heads, editable=8, files=0, passwords=0, buttons=("Save and Continue",),
         text="", errors=()):
    return {
        "url": "https://acme.wd5.myworkdayjobs.com/careers/job/1/apply",
        "title": "Apply", "heads": list(heads), "text": text or " ".join(heads),
        "buttons": [{"text": b, "disabled": False, "x": 10, "y": 20, "top": 500}
                    for b in buttons],
        "editable": editable, "files": files, "passwords": passwords,
        "errors": list(errors),
    }


# -- what kind of page is this? -------------------------------------------

def test_the_stages_it_can_name():
    assert workday.stage_of(page(["Sign In"], editable=3, passwords=1)) == "account"
    assert workday.stage_of(page(["Verify Your Email"], text="verification code")) == "verify"
    assert workday.stage_of(page(["Review"], editable=1, buttons=("Submit",))) == "review"
    assert workday.stage_of(page(["Associate Software Engineer"], editable=1,
                                 buttons=("Apply",))) == "posting"
    assert workday.stage_of(page(["My Information"], editable=9)) == "form"


def test_the_next_button_is_never_a_submit():
    look = page(["My Experience"], buttons=("Submit", "Save and Continue"))
    assert workday.next_button(look)["text"] == "Save and Continue"
    assert workday.next_button(page(["Review"], buttons=("Submit",))) is None


def test_what_workday_is_complaining_about():
    look = page(["My Information"], errors=["Email Address (needs a value)", "hello",
                                            "Please enter a phone number"])
    assert workday.blocking(look) == ["Email Address (needs a value)",
                                      "Please enter a phone number"]


# -- the walk -------------------------------------------------------------

class Tenant:
    """A scripted Workday: each look is served once, then the next."""

    def __init__(self, looks, controls=None):
        self.looks = list(looks)
        self.at = 0
        self.controls = controls or []
        self.clicks = []
        self.uploaded = []

    async def evaluate(self, expression):
        if "elementFromPoint" in expression:
            return True
        if "START" in expression and "getBoundingClientRect" in expression:
            return self.controls
        return self.looks[min(self.at, len(self.looks) - 1)]

    async def send(self, method, params=None):
        if method == "Input.dispatchMouseEvent" and (params or {}).get("type") == "mousePressed":
            self.clicks.append(params)
            self.at = min(self.at + 1, len(self.looks) - 1)
        return {}


@pytest.fixture
def tenant(monkeypatch):
    def build(looks, controls=None, documents=None):
        t = Tenant(looks, controls)

        @asynccontextmanager
        async def attached(cdp_url, url="", target_id=""):
            yield t, target_id or "TAB"

        async def in_tab(cdp_url, target_id):
            return None

        async def run_documents(page_, adapter, target_id="", resume=None, cover_letter=None,
                                answerer=None, corrections=None):
            t.uploaded.append(bool(resume))
            from browser.forms import Report
            out = Report()
            out.resume_uploaded = bool(resume)
            return out

        import browser.autofill as autofill_mod
        import browser.forms.engine as engine_mod
        monkeypatch.setattr(autofill_mod, "attached", attached)
        monkeypatch.setattr(autofill_mod, "in_tab", in_tab)
        monkeypatch.setattr(engine_mod, "run_documents", documents or run_documents)
        monkeypatch.setattr(workday, "SETTLE", 0)
        monkeypatch.setattr(workday, "ADVANCE_TIMEOUT", 0.05)
        monkeypatch.setattr(workday, "SETTLE_TIMEOUT", 0.05)
        return t
    return build


def walk(**kw):
    return asyncio.run(workday.walk("http://x", "TAB", **kw))


def test_it_walks_to_the_review_page_and_stops_there(tenant, tmp_path):
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF")
    t = tenant([
        page(["My Information"], editable=9),
        page(["My Experience"], editable=12, files=1),
        page(["Application Questions"], editable=6),
        page(["Voluntary Disclosures"], editable=4),
        page(["Review"], editable=1, buttons=("Submit",)),
    ])
    result = walk(resume=resume)
    assert result.reached_review and not result.paused
    assert result.resume_uploaded
    # The resume went on the one page with a slot, and nowhere else.
    assert t.uploaded == [False, True, False, False]
    # Nothing on the review page was pressed.
    assert len(t.clicks) == 4


def test_it_stops_at_the_account_and_says_whose_turn_it_is(tenant):
    t = tenant([page(["Sign In", "Create Account"], editable=3, passwords=1)])
    result = walk()
    assert result.paused == "needs_sign_in" and "sign in" in result.detail
    assert t.clicks == []


def test_it_stops_at_an_email_it_will_not_read(tenant):
    tenant([page(["Verify Your Email"], editable=1, text="we sent a verification code")])
    assert walk().paused == "verify_email"


def test_a_page_that_will_not_advance_says_what_workday_wants(tenant):
    """The same page twice: the click landed, nothing moved, and Workday's
    own error slots are the only honest explanation."""
    stuck = page(["My Information"], editable=9, errors=["Phone Number (needs a value)"])
    tenant([stuck, stuck, stuck, stuck])
    result = walk()
    assert result.paused == "blocked"
    assert result.needs == ["Phone Number (needs a value)"]


def test_a_posting_has_its_apply_pressed(tenant):
    t = tenant([
        page(["Associate Software Engineer"], editable=1, buttons=("Apply",)),
        page(["Sign In"], editable=3, passwords=1),
    ], controls=[{"text": "Apply", "shown": True, "disabled": False, "top": 1, "x": 5, "y": 6}])
    result = walk()
    assert t.clicks, "Apply was never pressed"
    assert result.paused == "needs_sign_in"


def test_an_application_that_goes_round_for_ever_stops(tenant):
    forever = page(["My Information"], editable=9)
    tenant([forever] * 3)
    result = walk(max_pages=3)
    assert result.paused in ("too_many_pages", "blocked", "stuck")


# -- nothing is pressed on a page that is still drawing itself ------------

def test_settled_waits_for_the_shape_to_stop_changing():
    """A Workday step replaces the page's contents without a navigation, so
    `document.readyState` never leaves "complete" and the first look after a
    step is of a page with no fields on it yet."""
    half = page(["My Experience"], editable=0, files=0)
    whole = page(["My Experience"], editable=12, files=1)
    t = Tenant([half, half, whole, whole, whole, whole])

    async def evaluate(expression):
        got = t.looks[min(t.at, len(t.looks) - 1)]
        t.at += 1
        return got
    t.evaluate = evaluate
    workday.SETTLE, keep = 0, workday.SETTLE
    try:
        look = asyncio.run(workday.settled(t, timeout=5))
    finally:
        workday.SETTLE = keep
    assert int(look["editable"]) == 12


def test_advance_hands_back_the_finished_page_not_the_first_different_one(monkeypatch):
    before = workday._shape(page(["My Information"], editable=9))
    half = page(["My Experience"], editable=0)
    whole = page(["My Experience"], editable=12, files=1)
    t = Tenant([half, whole, whole, whole])

    async def evaluate(expression):
        got = t.looks[min(t.at, len(t.looks) - 1)]
        t.at += 1
        return got
    t.evaluate = evaluate
    monkeypatch.setattr(workday, "SETTLE", 0)
    look = asyncio.run(workday.advance(t, before, timeout=5))
    assert int(look["editable"]) == 12


def test_the_review_page_says_which_resume_it_is_carrying(tenant, tmp_path, monkeypatch):
    """Every page of a Workday application replaces the last, so the logo
    the upload put on page two is gone by the review - and the review is
    exactly where "is this my tailored resume?" is asked."""
    resume = tmp_path / "Bhuvan_Resume.pdf"
    resume.write_bytes(b"%PDF")
    t = tenant([
        page(["My Information"], editable=9),
        page(["My Experience"], editable=12, files=1),
        page(["Review"], editable=1, buttons=("Submit",)),
    ])
    marked = []

    async def mark_named_file(page_, name, note, ref=""):
        marked.append((name, note))
        return True
    import browser.forms.engine as engine_mod
    monkeypatch.setattr(engine_mod, "mark_named_file", mark_named_file)
    result = walk(resume=resume)
    assert result.reached_review
    assert marked == [("Bhuvan_Resume.pdf", "the tailored resume")]
    assert result.marked == ["Bhuvan_Resume.pdf"]


def _drip(t, blank, form, after=2):
    """A page that arrives in pieces: `after` looks of nothing, then the
    form, for as long as anybody keeps looking."""
    calls = {"n": 0}

    async def evaluate(expression):
        if "elementFromPoint" in expression:
            return True
        if "START" in expression and "getBoundingClientRect" in expression:
            return t.controls
        calls["n"] += 1
        return blank if calls["n"] <= after else form
    t.evaluate = evaluate
    return calls


def test_the_first_page_is_read_only_once_it_has_finished_drawing(tenant, monkeypatch):
    """The tab has just been opened, or has just come back from Apply. A
    Workday page arrives in pieces, and the piece that arrives first has no
    heading and no fields - which is not a page the walk can name."""
    monkeypatch.setattr(workday, "SETTLE_TIMEOUT", 5.0)
    t = tenant([page(["My Experience"], editable=12, files=1)])
    _drip(t, page([], editable=0, buttons=()), page(["My Experience"], editable=12, files=1))
    result = walk()
    assert result.pages, "the walk gave up before it read a page"
    assert result.pages[0].heading == "My Experience"


def test_jobrights_autofill_is_not_pressed_into_a_half_drawn_page(tenant, monkeypatch):
    """An autofill pressed with no job matched is the "Autofill for Another
    Job" control, and that one opens a tab of its own - which is where the
    blank tabs came from. Three runs on disk record "pressed by code,
    0 - 13 fields" on pages two, three and four."""
    monkeypatch.setattr(workday, "SETTLE_TIMEOUT", 5.0)
    t = tenant([page(["My Experience"], editable=12, files=1)])
    blank = page([], editable=0, buttons=())
    form = page(["My Experience"], editable=12, files=1)
    calls = _drip(t, blank, form)
    pressed_at = []

    async def in_tab(cdp_url, target_id):
        pressed_at.append(blank["editable"] if calls["n"] <= 2 else form["editable"])
        return None
    import browser.autofill as autofill_mod
    monkeypatch.setattr(autofill_mod, "in_tab", in_tab)
    walk()
    assert pressed_at and all(n > 0 for n in pressed_at), pressed_at
