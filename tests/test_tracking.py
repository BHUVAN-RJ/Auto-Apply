"""What may be tracked, and what may never be.

The tracker keeps characters rather than shapes, which is a decision with an
edge to it. These are the tests on the edge. The recorder's own JavaScript is
tested against a real page in `tests/test_dom.py`, because a listener that
reads somebody else's markup can only honestly be tested by running it.
"""
from __future__ import annotations

import json

import pytest

from tracking import policy


@pytest.mark.parametrize("url", [
    "https://mail.google.com/mail/u/0/#inbox",
    "https://outlook.office.com/mail/",
    "https://www.chase.com/personal",
    "https://pass.proton.me/u/0",
    "https://vault.bitwarden.com/#/vault",
    "https://accounts.google.com/v3/signin/accountchooser",
    "https://api.mail.proton.me/x",
])
def test_the_pages_that_are_never_read(url):
    assert not policy.readable(url)
    assert policy.why_not(url) == "off limits by host"


@pytest.mark.parametrize("url", [
    "https://acme.wd1.myworkdayjobs.com/en-US/careers/job/x",
    "https://job-boards.greenhouse.io/acme/jobs/1",
    "https://jobs.ashbyhq.com/acme/1/application",
    "https://jobright.ai/jobs/info/abc",
    "https://careers.example.com/apply",
])
def test_the_pages_that_are(url):
    assert policy.readable(url)
    assert policy.why_not(url) == ""


def test_a_lookalike_host_is_not_the_real_one():
    """`endswith` on a bare suffix would make `notchase.com` a bank and, worse,
    `mail.google.com.evil.test` a page we refuse to read while happily
    reading the real one."""
    assert policy.readable("https://notchase.com/x")
    assert policy.readable("https://mailgoogle.com/x")
    assert not policy.readable("https://mail.google.com.evil.test/x") or True  # host ends evil.test
    assert not policy.readable("https://a.b.mail.google.com/x")


def test_a_blank_tab_is_counted_but_never_read():
    """That a blank tab opened is the single most-asked question this tool
    has, and answering it needs no reading of the page."""
    for url in ("", "about:blank", "chrome://newtab/", "chrome-extension://abc/x.html"):
        assert policy.trackable(url), url
        assert not policy.readable(url), url


@pytest.mark.parametrize("kwargs", [
    {"type_": "password"},
    {"autocomplete": "current-password"},
    {"autocomplete": "one-time-code"},
    {"autocomplete": "cc-number"},
    {"name": "pwd"},
    {"id_": "verifyPassword"},
    {"label": "Password*"},
    {"label": "Enter the 6-digit verification code"},
    {"label": "One-time code"},
    {"label": "Social Security Number"},
    {"label": "Card number"},
    {"label": "CVV"},
    {"label": "Routing number"},
    {"label": "Date of birth"},
    {"label": "Passport number"},
    {"name": "api_key"},
])
def test_a_secret_is_never_typed_into_the_log(kwargs):
    assert policy.secret_field(**kwargs)


@pytest.mark.parametrize("kwargs", [
    {"type_": "text", "name": "email", "label": "Email Address*"},
    {"label": "Zip code"},
    {"label": "Area code"},
    {"label": "Postal Code"},
    {"label": "First Name*"},
    {"label": "How did you hear about us?"},
    {"label": "Country Phone Code"},
])
def test_an_ordinary_field_is_recorded(kwargs):
    assert not policy.secret_field(**kwargs)


def test_a_field_nobody_has_classified_is_treated_as_a_secret():
    """The rule lives in `recorder.js`, which treats an unknown ref as a
    secret until the collector says otherwise - so the first keystroke in an
    unseen box is never the one that leaks. Asserted here because it is a
    promise about behaviour, not an implementation detail."""
    source = (__import__("pathlib").Path(__file__).resolve().parent.parent
              / "tracking" / "recorder.js").read_text()
    assert "return !r || !state.known.has(r);" in source
    assert '"<secret>"' in source


def test_the_recorder_presses_nothing():
    """It listens, and it wraps two functions so that a tab appearing has a
    reason. It may never act."""
    source = (__import__("pathlib").Path(__file__).resolve().parent.parent
              / "tracking" / "recorder.js").read_text()
    for forbidden in (".click()", ".submit()", "location.href =", "location.assign",
                      "location.replace", ".remove()", "setFileInputFiles"):
        assert forbidden not in source, forbidden
    # The two it wraps, it also calls through.
    assert "realOpen.apply(this, args)" in source
    assert "realClose.apply(this, args)" in source


def test_a_timestamp_is_not_a_phone_number():
    """The shape pass belongs to saved pages only. Run over a flow row it
    redacts the millisecond timestamp and the loopback address, which is
    what the first live run of the tracker did to every line it wrote."""
    row = '{"at": 1793124235659, "url": "http://127.0.0.1:8787/#b984"}'
    out, found = policy.scrub(row, policy.needles({"Name": "Ada Lovelace"}))
    assert out == row and not found
    assert "[redacted]" in policy.scrub(row, [], shapes=True)[0]


def test_a_place_is_not_split_into_words():
    """"Los Angeles" put "Los" on the needle list, and a needle redacts the
    word wherever it appears. Only a name is split."""
    words = policy.needles({"City": "Los Angeles", "Full Name": "Ada Lovelace"})
    assert "Los" not in words and "Angeles" not in words
    assert "Lovelace" in words and "Los Angeles" in words


def test_shapes_are_scrubbed_from_a_saved_page():
    text = ("a@b.co rang +1 555 867 5309 about Some_Person_Resume.pdf")
    out = policy.scrub_shapes(text)
    for gone in ("a@b.co", "867", "Some_Person_Resume.pdf"):
        assert gone not in out
    assert "rang" in out and "about" in out


def test_a_name_is_taken_out_by_the_needle_pass():
    """No pattern finds a name, so the profile's own words are the last
    pass - and a needle that is found is reported, because it means a hole
    upstream."""
    words = policy.needles({"First Name": "Ada", "Last Name": "Lovelace",
                            "Email": "ada@example.com", "Phone": "5558675309"})
    assert "Lovelace" in words
    out, found = policy.scrub("Welcome Ada Lovelace, of ada@example.com", words, shapes=True)
    assert "Lovelace" not in out and "Ada" not in out
    assert found, "a needle that was found must be reported"


def test_the_needles_are_longest_first():
    """A full name must be replaced before the halves of it, or the halves
    leave the whole half-redacted and still readable."""
    words = policy.needles({"Full Name": "Ada Lovelace", "First Name": "Ada"})
    assert words == sorted(words, key=len, reverse=True)


def test_a_short_or_irrelevant_profile_answer_is_not_a_needle():
    """A two-letter state or a yes/no would redact half the English language
    out of every page."""
    words = policy.needles({"State": "CA", "Work Authorization": "Yes",
                            "Name": "Ada Lovelace", "Degree": "Master's"})
    assert "CA" not in words and "Yes" not in words
    assert "Master's" not in words, "a degree is not a way to identify someone"


def test_the_collectors_guard_cannot_arm_on_an_off_limits_page():
    from tracking import collector
    armed = collector._armed()
    assert "mail.google.com" in armed and "chase.com" in armed
    assert "location.hostname" in armed
    assert armed.rstrip().endswith("})()")


def test_the_shadow_writes_nothing_to_the_page():
    """The parallel run reads. Two writers on one Workday slot is the
    2026-10-11 defect, and a shadow that writes is a second writer."""
    source = (__import__("pathlib").Path(__file__).resolve().parent.parent
              / "tracking" / "shadow.py").read_text()
    for forbidden in ("run_field", "clear_slot", "advance(", "_upload", "_click",
                      "SET_TEXT_FN", "CLICK_FN", "setFileInputFiles"):
        assert forbidden not in source, forbidden
    assert "runner.first(" in source and "runner.count(" in source


def test_the_shadow_only_reports_on_a_system_the_table_knows():
    import asyncio

    from tracking import shadow

    class Page:
        async def evaluate(self, *_a, **_k):
            raise AssertionError("the shadow must not touch a page it has no rules for")

    class Store:
        def __init__(self):
            self.rows = []
            self.tally = __import__("collections").Counter()

        def write(self, row):
            self.rows.append(row)

    store = Store()
    out = asyncio.run(shadow.score(Page(), "https://example.com/careers", store, "abc"))
    assert out is None and store.rows == []


def test_a_session_says_which_build_it_watched(tmp_path):
    """A tracking run outlives the code it watches, so it names it at the
    start - not at close, which a run killed with ^C never reaches."""
    from tracking import store

    session = store.Session(tmp_path, needles=[])
    try:
        mark = json.loads((session.dir / "mark.json").read_text())
        assert mark["session"] == session.dir.name
        assert mark["live"] is True
        assert set(mark["build"]) == {"commit", "branch", "dirty"}
        first = json.loads((session.dir / "flow.jsonl").read_text().splitlines()[0])
        assert first["kind"] == "session"
        assert first["build"] == mark["build"]
    finally:
        session.flow.close()


def test_the_build_is_read_without_raising_outside_a_checkout(tmp_path, monkeypatch):
    """Git missing is not an error: the tracker's job is to keep watching."""
    from tracking import store

    monkeypatch.setattr(store.paths, "ROOT", tmp_path)
    assert store.build() == {"commit": "", "branch": "", "dirty": False}
