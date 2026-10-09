"""The two detectors, asked separately, and the tab that is followed.

No browser here: `looks_like_signin` and `looks_like_form` are pure, which
is the whole reason they were split out of the fill. What they are asked
about is one `Look` at a tab, and the cases below are the pages that made
the split necessary - a Workday sign-in with six boxes on it, Google's own
consent screen in the middle of a single sign-on, and the application form
that appears afterwards at a URL nobody predicted.
"""

from __future__ import annotations

from browser import signin


def look(**kw) -> signin.Look:
    base = {"url": "https://acme.wd1.myworkdayjobs.com/en-US/careers/job/1/apply",
            "title": "Apply", "text": "", "fields": 0, "files": 0, "passwords": 0,
            "apply": 0}
    base.update(kw)
    return signin.Look(**base)


# -- detector one: is this a sign-in? -------------------------------------

def test_a_password_box_is_decisive():
    assert signin.looks_like_signin(look(passwords=1, fields=2))


def test_workdays_sign_in_page_is_one_even_with_six_boxes():
    """The gate that was wrong before wanted fewer than five fields, and
    Workday's sign-in has six: email, password, verify, a checkbox and two
    buttons. So the fill treated a login screen as the application."""
    page = look(text="Sign In\nEmail Address\nPassword\nVerify New Password\nRemember me",
                fields=4, passwords=2)
    assert signin.looks_like_signin(page)
    assert not signin.looks_like_form(page)


def test_single_sign_on_is_a_sign_in_wherever_it_has_gone():
    for url in ("https://accounts.google.com/o/oauth2/v2/auth?client_id=x",
                "https://login.microsoftonline.com/common/oauth2/authorize",
                "https://acme.okta.com/login/login.htm",
                "https://id.workday.com/sign-in"):
        assert signin.identity_host(url), url
        assert signin.looks_like_signin(look(url=url, text="Choose an account"))


def test_a_posting_that_mentions_logging_in_is_not_a_sign_in():
    """The word is everywhere. A page offering an account has almost nothing
    else on it; a job description mentioning a portal does not count."""
    page = look(text="You will build the customer portal where users log in to manage "
                     "their orders. Requirements: three years of TypeScript...",
                fields=0)
    assert not signin.looks_like_signin(page)


def test_a_form_with_sign_in_in_its_header_is_still_a_form():
    page = look(text="Sign in\nApply for this job\nFirst name\nLast name\nResume",
                fields=9, files=1)
    assert not signin.looks_like_signin(page)
    assert signin.looks_like_form(page)


# -- detector two: is this the form? --------------------------------------

def test_somewhere_to_put_the_resume_is_enough():
    assert signin.looks_like_form(look(files=1, fields=1))


def test_enough_real_fields_is_enough_when_there_is_no_upload_yet():
    """Workday's first page asks how you heard about them and for your
    country before it ever offers a file slot."""
    assert signin.looks_like_form(look(fields=signin.FORM_FIELDS))
    assert not signin.looks_like_form(look(fields=signin.FORM_FIELDS - 1))


def test_the_provider_s_own_page_is_never_the_application():
    """Google's account chooser has boxes on it. It is not the form."""
    page = look(url="https://accounts.google.com/signin/v2/identifier", fields=8, files=1)
    assert not signin.looks_like_form(page)


def test_the_form_may_live_on_another_host_than_the_posting():
    """A careers page links to the employer's Workday tenant. Refusing the
    form for that is the round trip this module exists to remove."""
    page = look(url="https://acme.wd5.myworkdayjobs.com/apply", files=1, fields=7)
    assert signin.looks_like_form(page, job_url="https://careers.acme.com/jobs/1")


def test_the_two_detectors_never_both_say_yes():
    for page in (look(passwords=1, fields=3), look(files=1, fields=9),
                 look(url="https://accounts.google.com/x", fields=6)):
        assert not (signin.looks_like_signin(page) and signin.looks_like_form(page))


# -- the joining: one tab, followed ---------------------------------------

class FakePage:
    """A tab that walks: posting → sign-in → Google → back → the form."""

    def __init__(self, looks):
        self.looks = list(looks)
        self.seen = 0

    async def evaluate(self, expression):
        raw = self.looks[min(self.seen, len(self.looks) - 1)]
        self.seen += 1
        return raw


def test_the_watch_follows_the_tab_through_the_provider_and_back(monkeypatch):
    import asyncio
    from contextlib import asynccontextmanager

    walk = [
        {"url": "https://acme.wd1.myworkdayjobs.com/job/1", "text": "Sign In", "fields": 2,
         "files": 0, "passwords": 1},
        {"url": "https://accounts.google.com/o/oauth2/auth", "text": "Choose an account",
         "fields": 1, "files": 0, "passwords": 0},
        {"url": "https://acme.wd1.myworkdayjobs.com/job/1/apply", "text": "My Information",
         "fields": 9, "files": 1, "passwords": 0},
    ]
    page = FakePage(walk)

    @asynccontextmanager
    async def attached(cdp_url, url="", target_id=""):
        yield page, target_id or "TAB1"
    monkeypatch.setattr(signin, "attached", attached)
    monkeypatch.setattr(signin, "POLL", 0)

    said = []
    result = asyncio.run(signin.wait_for_form(
        "http://127.0.0.1:9333", "TAB1", "https://acme.wd1.myworkdayjobs.com/job/1",
        timeout=5, notify=lambda line: said.append(line)))
    assert result.ready and result.saw_signin and result.signed_in
    assert result.target_id == "TAB1"        # the same tab throughout
    assert "sign in" in " ".join(said).lower()


def test_a_wall_that_never_clears_gives_up_and_says_so(monkeypatch):
    import asyncio
    from contextlib import asynccontextmanager

    page = FakePage([{"url": "https://acme.wd1.myworkdayjobs.com/job/1", "text": "Sign In",
                      "fields": 2, "files": 0, "passwords": 1}])

    @asynccontextmanager
    async def attached(cdp_url, url="", target_id=""):
        yield page, target_id
    monkeypatch.setattr(signin, "attached", attached)
    monkeypatch.setattr(signin, "POLL", 0)

    result = asyncio.run(signin.wait_for_form("http://x", "TAB1", timeout=0.05))
    assert not result.ready and result.saw_signin
    assert "signed in" in result.note


def test_the_words_alone_decide_nothing(monkeypatch):
    """"Log in" is in half the job descriptions in the country. The phrase
    has to be the whole line - a heading or a button - or be one nobody
    writes in prose ("Continue with Google", "Forgot your password")."""
    buried = look(text="Candidates log in to our partner portal daily. Sign in flows are "
                       "part of the platform you would own.", fields=2)
    assert not signin.looks_like_signin(buried)
    on_its_own = look(text="Acme Careers\nSign In\nEmail Address", fields=2)
    assert signin.looks_like_signin(on_its_own)
    never_prose = look(text="Welcome back to the careers portal\nForgot your password?", fields=2)
    assert signin.looks_like_signin(never_prose)


def test_a_resume_slot_outranks_any_header():
    """A page you can attach a resume to is the application, whatever the
    sign-in offer above it says: Greenhouse and Ashby both put one there."""
    assert not signin.looks_like_signin(look(text="Sign In", files=1, fields=6))


# -- the posting that nobody was pressing Apply on ------------------------

class PressablePage(FakePage):
    """A Workday posting whose Apply leads to the account page."""

    def __init__(self, looks, controls):
        super().__init__(looks)
        self.controls = controls
        self.presses = []

    async def evaluate(self, expression):
        if "elementFromPoint" in expression:
            return True
        if "getBoundingClientRect" in expression and "START" in expression:
            return self.controls
        return await super().evaluate(expression)

    async def send(self, method, params=None):
        if method == "Input.dispatchMouseEvent" and params.get("type") == "mousePressed":
            self.presses.append(params)
        return {}


def _watch(monkeypatch, page, **kw):
    import asyncio
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def attached(cdp_url, url="", target_id=""):
        yield page, target_id or "TAB1"
    monkeypatch.setattr(signin, "attached", attached)
    monkeypatch.setattr(signin, "POLL", 0)
    return asyncio.run(signin.wait_for_form("http://x", "TAB1", "https://acme.wd5.myworkdayjobs.com/job/1", **kw))


def test_a_postings_apply_is_pressed_so_the_account_can_be_offered(monkeypatch):
    """The Globus Medical run: the tab sat on the posting for the full ten
    minutes because the sign-in it was waiting for cannot appear until Apply
    is pressed."""
    posting = {"url": "https://acme.wd5.myworkdayjobs.com/job/1", "text": "Associate Software Engineer",
               "fields": 1, "files": 0, "passwords": 0}
    account = {"url": "https://acme.wd5.myworkdayjobs.com/job/1/apply", "text": "Sign In\nCreate Account",
               "fields": 3, "files": 0, "passwords": 1}
    form = {"url": "https://acme.wd5.myworkdayjobs.com/job/1/apply/2", "text": "My Information",
            "fields": 9, "files": 1, "passwords": 0}
    page = PressablePage([posting, account, form],
                         [{"text": "Apply", "shown": True, "disabled": False,
                           "top": 100, "x": 40, "y": 60}])
    result = _watch(monkeypatch, page, timeout=5)
    assert result.applied == "Apply" and result.pressed == 1
    assert len(page.presses) == 1
    assert result.ready and result.saw_signin


def test_a_posting_nobody_is_watching_stops_holding_the_fill_queue(monkeypatch):
    """A wall somebody is standing in front of gets the full ten minutes. A
    page where nothing is happening gets `NO_SIGNIN_TIMEOUT`, because the
    fills run one at a time and this one is going nowhere."""
    posting = {"url": "https://acme.wd5.myworkdayjobs.com/job/1", "text": "A job",
               "fields": 1, "files": 0, "passwords": 0}
    page = PressablePage([posting], [])
    monkeypatch.setattr(signin, "NO_SIGNIN_TIMEOUT", 0.01)
    result = _watch(monkeypatch, page, timeout=600)
    assert not result.ready and not result.saw_signin
    assert result.waited < 5 and "no application form" in result.note


def test_the_apply_press_can_be_switched_off(monkeypatch):
    posting = {"url": "https://acme.wd5.myworkdayjobs.com/job/1", "text": "A job",
               "fields": 1, "files": 0, "passwords": 0}
    page = PressablePage([posting], [{"text": "Apply", "shown": True, "disabled": False,
                                      "top": 1, "x": 1, "y": 1}])
    monkeypatch.setattr(signin, "NO_SIGNIN_TIMEOUT", 0.01)
    result = _watch(monkeypatch, page, timeout=600, press_apply=False)
    assert page.presses == [] and result.pressed == 0


def test_a_dropped_connection_is_retried_once(monkeypatch):
    """Two of the three Greenhouse jobs on the failed shelf died on the CDP
    socket, not on the form: "no close frame received or sent" and "Session
    with given id not found". The tab was fine; the connection was not."""
    import asyncio

    from browser import fill

    calls = []

    async def press(cdp_url, url):
        calls.append(url)
        if len(calls) == 1:
            raise RuntimeError("no close frame received or sent")
        return fill.autofill.AutofillResult(note="pressed")

    monkeypatch.setattr(fill.autofill, "press", press)
    monkeypatch.setattr(fill, "RETRY_WAIT", 0)
    result = asyncio.run(fill._press_again("http://x", "https://job-boards.greenhouse.io/embed/job_app"))
    assert len(calls) == 2 and result.note == "pressed"


def test_a_real_failure_is_not_retried(monkeypatch):
    """Only the transport is worth a second attempt; a page that has no
    Autofill control will not grow one in two seconds."""
    import asyncio

    from browser import fill

    calls = []

    async def press(cdp_url, url):
        calls.append(url)
        raise RuntimeError("no Autofill control on the page")

    monkeypatch.setattr(fill.autofill, "press", press)
    monkeypatch.setattr(fill, "RETRY_WAIT", 0)
    result = asyncio.run(fill._press_again("http://x", "https://example.com/job"))
    assert len(calls) == 1 and "no Autofill control" in result.note


def test_a_failure_in_the_documents_step_is_never_silent(monkeypatch):
    """It went into `Report.note`, which nothing logged, so a Greenhouse job
    that died here said "resume NOT attached" with no errors beside it and no
    way to tell why (Canonical, `f7a6`)."""
    import asyncio

    from browser import fill

    async def boom():
        raise RuntimeError("Session with given id not found.")

    with_retry = []

    async def run():
        with_retry.append(1)
        await boom()

    try:
        asyncio.run(fill._twice("documents", run))
    except RuntimeError as error:
        assert "Session with given id" in str(error)
    else:
        raise AssertionError("the error must reach the caller")
    assert len(with_retry) == 2, "a dropped session is retried once"


def test_a_posting_with_a_sign_in_link_in_its_header_is_not_a_wall():
    """Every Workday tenant puts "Sign In" in its header. The Globus Medical
    posting had it, no fields at all, and an Apply button - and the words
    alone made the watch call it a sign-in, so it waited for someone to get
    through a wall that was not there while the Apply on the same page was
    never pressed."""
    posting = look(text="Sign In\nApply\nAssociate Software Engineer\nAudubon, PA",
                   fields=0, apply=1)
    assert not signin.looks_like_signin(posting)
    from browser import open_apply
    assert open_apply.safe_to_press(posting)


def test_the_account_page_is_still_a_wall_even_with_apply_on_it():
    """A password box is decisive, whatever else the page offers."""
    page = look(text="Sign In\nApply\nPassword", fields=3, passwords=1, apply=1)
    assert signin.looks_like_signin(page)


def test_nothing_is_pressed_before_the_form_is_on_the_screen():
    """The watch answers "is the application up?" and the fill used to press
    on regardless of the answer. On a Workday posting that meant opening a
    second tab and pressing Jobright's "Autofill for Another Job" against a
    job description - the stray tabs - and on a sign-in it meant working a
    page the person had not finished with."""
    source = (__import__("server.runner", fromlist=["ROOT"]).ROOT / "browser" / "fill.py").read_text()
    assert "form_is_up = gate is None or gate.ready" in source
    assert "if (walk is None and easy is None and form_is_up and not filled.attempted" in source
    # And the job comes back as one needing a person, not a failure.
    assert "filled.errors.append(forms.NOT_A_FORM)" in source

def test_a_patient_watch_waits_out_a_wall_it_cannot_see(monkeypatch):
    """Workday's posting header carries "Sign In" on every tenant, so the
    wall is never recognised there - and since 2026-10-09 Apply is not
    pressed on Workday either, so the account page it leads to never
    appears while we are looking. The short no-wall cutoff was therefore
    answering "no application form on this page" while the person was still
    typing their password. `patient` gives the page the whole timeout and
    says what it is waiting for."""
    posting = {"url": "https://acme.wd5.myworkdayjobs.com/job/1", "text": "A job",
               "fields": 1, "files": 0, "passwords": 0}
    form = {"url": "https://acme.wd5.myworkdayjobs.com/job/1/apply", "text": "My Information",
            "fields": 9, "files": 1, "passwords": 0}
    page = PressablePage([posting, posting, form], [])
    monkeypatch.setattr(signin, "NO_SIGNIN_TIMEOUT", 0.01)
    said = []
    result = _watch(monkeypatch, page, timeout=600, press_apply=False, patient=True,
                    notify=said.append)
    # It did not give up at the short cutoff, and nothing was pressed.
    assert result.ready, result.note
    assert page.presses == []
    assert said and "sign in" in said[0].lower()


def test_a_patient_watch_still_gives_the_queue_its_tab_back(monkeypatch):
    """Patient is not for ever: the fills run one at a time, so the timeout
    is the cap and the note says what was being waited for."""
    posting = {"url": "https://acme.wd5.myworkdayjobs.com/job/1", "text": "A job",
               "fields": 1, "files": 0, "passwords": 0}
    page = PressablePage([posting], [])
    result = _watch(monkeypatch, page, timeout=0.05, press_apply=False, patient=True)
    assert not result.ready
    assert "waiting to be signed in" in result.note
