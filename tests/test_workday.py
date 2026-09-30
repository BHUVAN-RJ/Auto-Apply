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
