"""The page scripts, run against real DOMs in a real browser.

Everywhere else the suite stubs the browser, and that is the right default -
but it is also why every bug in this file survived a green suite and was
found on a live form instead. `browser/forms/engine.py` and `putFile` in
`capture/content.js` are JavaScript that reads somebody else's markup, and
the only honest test of that is to run it against the markup. The fixtures
in `tests/dom/` are the shapes the real forms have; `ashby_application.html`
is the live form's own markup, copied on 2026-10-06.

A Chromium is needed. There is none on a machine that has not run the app,
so these skip rather than fail there - `scripts/check.sh` on a working
install is where they count.
"""

from __future__ import annotations

import asyncio
import json
import socket
import tempfile
from pathlib import Path

import pytest

from browser import autofill, chrome
from browser.forms import engine
from browser.forms.engine import Adapter, Engine, Profile

DOM = Path(__file__).resolve().parent / "dom"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="session")
def browser():
    """A headless Chrome of this test run's own, on its own port and its own
    profile: Chrome will not share a profile directory with a running
    instance, and the person's browser is not ours to drive."""
    if chrome.find_browser() is None:
        pytest.skip("no Chrome or Brave installed")
    directory = Path(tempfile.mkdtemp(prefix="autopilot-dom-")) / "chrome"
    port = _free_port()
    try:
        cdp = chrome.launch(directory, port, headless=True)
    except chrome.ChromeError as error:  # pragma: no cover - a broken install
        pytest.skip(f"Chrome would not start: {error}")
    try:
        yield cdp
    finally:
        chrome.close(port)


def _open(cdp: str, name: str) -> str:
    """A new tab on a fixture page; returns its target id."""
    import urllib.parse
    import urllib.request
    url = f"file://{DOM / name}"
    request = urllib.request.Request(
        f"{cdp}/json/new?{urllib.parse.quote(url, safe='')}", method="PUT")
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read())["id"]


def on_page(cdp: str, name: str, work):
    """Run `work(engine, page)` on a tab showing `name`, then close it."""
    async def go():
        target = _open(cdp, name)
        try:
            async with autofill.attached(cdp, target_id=target) as (page, _):
                await page.send("DOM.enable")
                await autofill.wait_for_load(page)
                return await work(Engine(page, Adapter(), Profile({})), page)
        finally:
            chrome.close_tab(target, int(cdp.rsplit(":", 1)[1]))
    return asyncio.run(go())


# -- Ashby: the slot that parses a resume is not a slot --------------------

def test_ashbys_parse_input_has_no_name_of_any_kind(browser):
    """Why `guard.describes_parse_slot` could not see it: the input carries
    no id, no name and no aria-label, and the engine's label walk reaches
    past it to the next field's. The sentence beside it is all there is."""
    async def work(e, page):
        files = [f for f in await e.wait_for_fields() if f.kind == "file"]
        return [(f.id, f.name, f.label, f.context) for f in files]
    files = on_page(browser, "ashby_application.html", work)
    assert len(files) == 2
    (pid, pname, plabel, pcontext), (rid, _, rlabel, _) = files
    assert (pid, pname, plabel) == ("", "", "")
    assert "Autofill from resume" in pcontext
    assert rid == "_systemfield_resume" and rlabel == "Resume"


def test_the_resume_goes_on_the_real_slot_not_the_autofill_one(browser):
    async def work(e, page):
        fields = await e.wait_for_fields()
        resume, cover = e.file_inputs(fields)
        return (resume.id if resume else None, cover.id if cover else None,
                list(e.report.skipped_parse))
    resume, cover, skipped = on_page(browser, "ashby_application.html", work)
    assert resume == "_systemfield_resume" and cover is None
    assert skipped and "Autofill from resume" in skipped[0]


def test_on_the_second_pass_the_parser_is_not_handed_the_resume(browser):
    """A file is on the real slot, Ashby has dropped its input, and the
    parser is the only file input left - which the one-input fallback used
    to hand the tailored resume to."""
    async def work(e, page):
        fields = await e.wait_for_fields()
        resume, cover = e.file_inputs(fields)
        return (resume, cover, list(e.report.skipped_parse))
    resume, cover, skipped = on_page(browser, "ashby_resume_taken.html", work)
    assert resume is None and cover is None
    assert skipped and "Autofill from resume" in skipped[0]


def test_the_put_chip_refuses_the_autofill_slot_too(browser):
    """The banner's chip picks the input itself, from the words round it -
    and that sentence says "resume" twice, so the parser was both first in
    document order and the best match."""
    source = (Path(__file__).resolve().parent.parent / "capture" / "content.js").read_text()
    put = source.split("function putFile(")[1].split("\nfunction ")[0]

    async def work(e, page):
        return await page.evaluate(
            "(() => { window.prompt = () => null;"
            f" function putFile({put}\n"
            " const f = new File([new Uint8Array([1])], 'tailored.pdf');"
            " const out = putFile(f, 'resume');"
            " const on = [...document.querySelectorAll('input[type=file]')]"
            "   .map((el) => (el.files && el.files.length) ? el.files[0].name : '');"
            " return {out: out, on: on}; })()")

    # On the real page the chip puts the file on the real slot.
    got = on_page(browser, "ashby_application.html", work)
    assert got["out"]["ok"] is True
    assert got["on"] == ["", "tailored.pdf"], got["on"]

    # With only the parser left it refuses rather than feeding it the resume.
    got = on_page(browser, "ashby_resume_taken.html", work)
    assert got["out"]["ok"] is False
    assert "not the resume slot" in got["out"]["note"]
    assert got["on"] == [""]


# -- the file already on the slot -----------------------------------------

def test_workdays_delete_is_found_though_the_input_is_still_there(browser):
    """Workday keeps the input beside the attachment, and the walk from the
    Delete button used to give up at the first ancestor holding one - so the
    old resume stayed on the form next to ours, with nothing to say which
    was sent."""
    async def work(e, page):
        return [
            await e.call(engine.FIND_REMOVE_FN, engine.RESUME_SLOT,
                         f"{engine.COVER_SLOT}|{engine.PARSE_SLOT}", engine.DELETE_LABELS),
            await e.call(engine.FIND_REMOVE_FN, engine.COVER_SLOT, engine.PARSE_SLOT,
                         engine.DELETE_LABELS),
        ]
    resume, cover = on_page(browser, "workday_resume_slot.html", work)
    assert resume and resume["title"] == "Resume/CV" and resume["label"] == "Delete"
    assert "Bhuvan_Rajanahally_Jayakumar_Resume.pdf" in resume["file"]
    # And the resume's Delete never answers for the cover letter's slot.
    assert cover and cover["title"] == "Cover Letter"
    assert "their_cover_letter.pdf" in cover["file"]


def test_greenhouse_still_works_where_the_input_is_dropped(browser):
    """The shape the step was written for, which the Workday fix must not
    cost: no input at all, a filename and a "Remove file" button."""
    async def work(e, page):
        return await e.call(engine.FIND_REMOVE_FN, engine.RESUME_SLOT,
                            f"{engine.COVER_SLOT}|{engine.PARSE_SLOT}", engine.REMOVE_LABELS)
    found = on_page(browser, "greenhouse_resume_taken.html", work)
    assert found and found["label"] == "Remove file"
    assert "Resume/CV" in found["title"]


def test_the_remove_control_is_read_before_it_is_pressed(browser):
    """`remove_attached` puts the button's own label through the submit
    guard. A page whose only clearing control reads as submit is left alone."""
    async def work(e, page):
        await page.evaluate(
            "document.querySelector('[aria-label=Delete]')"
            ".setAttribute('aria-label', 'Delete and submit application')")
        done = await e.remove_attached(engine.RESUME_SLOT,
                                       f"{engine.COVER_SLOT}|{engine.PARSE_SLOT}",
                                       engine.DELETE_LABELS)
        return done, list(e.report.errors)
    done, errors = on_page(browser, "workday_resume_slot.html", work)
    assert done is False
    assert errors and "refused to press" in errors[0]


# -- the review page, which has the file and nothing else ------------------

def test_the_review_page_can_still_say_which_resume_it_is_carrying(browser):
    """No input, no tagged block, no ref: by the review page they belong to
    a page Workday has replaced. The filename is what is left."""
    name = "Bhuvan_Rajanahally_Jayakumar_Resume.pdf"

    async def work(e, page):
        placed = await engine.mark_named_file(page, name, "the tailored resume")
        shown = await page.evaluate(
            "(() => { const d = document.querySelector('[data-autopilot-mark]');"
            " return d ? {title: d.title, host: d.parentElement.innerText.trim(),"
            " first: d.parentElement.firstChild === d} : null; })()")
        return placed, shown
    placed, shown = on_page(browser, "workday_review.html", work)
    assert placed is True
    assert shown["title"] == "Filled by Autopilot: the tailored resume"
    assert shown["host"] == name and shown["first"] is True


def test_the_logo_goes_on_the_name_and_not_the_whole_page(browser):
    """The smallest element holding the filename, so the mark sits on the
    row rather than on the body."""
    async def work(e, page):
        await engine.mark_named_file(page, "Bhuvan_Rajanahally_Jayakumar_Resume.pdf",
                                     "the tailored resume")
        return await page.evaluate(
            "document.querySelectorAll('[data-autopilot-mark]').length")
    assert on_page(browser, "workday_review.html", work) == 1


def test_a_name_that_is_not_on_the_page_marks_nothing(browser):
    async def work(e, page):
        return await engine.mark_named_file(page, "somebody_elses.pdf", "the tailored resume")
    assert on_page(browser, "workday_review.html", work) is False
