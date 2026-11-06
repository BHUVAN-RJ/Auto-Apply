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
import re
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
    """Run `work(engine, page)` on a tab showing `name`, then close it.

    Once more if the CDP socket drops. `attached` opens a websocket to the
    browser for every call and these tests make twenty of them; one in
    twenty comes back "no close frame received or sent", which is the drop
    `browser/fill.py` already retries a real fill through. A test that fails
    one run in twenty teaches nobody anything, and `fill.TRANSPORT` is the
    one definition of what a dropped connection reads like.
    """
    from browser.fill import TRANSPORT

    async def go():
        target = _open(cdp, name)
        try:
            async with autofill.attached(cdp, target_id=target) as (page, _):
                await page.send("DOM.enable")
                await autofill.wait_for_load(page)
                return await work(Engine(page, Adapter(), Profile({})), page)
        finally:
            chrome.close_tab(target, int(cdp.rsplit(":", 1)[1]))

    for attempt in (1, 2):
        try:
            return asyncio.run(go())
        except Exception as error:  # noqa: BLE001 - re-raised unless it is the socket
            if attempt == 2 or not TRANSPORT.search(str(error)):
                raise


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


def put_source() -> str:
    """The chip's own code, from `const SLOT_CLIMB` through the end of
    `putFile`: the constants, the slot namer, the clearer and the putter
    together, so what the test runs is what ships rather than a copy of it.
    Taken as one region because `putFile` awaits `clearSlot` and reads
    `slotWords`, and none of the three works without the others."""
    source = (Path(__file__).resolve().parent.parent / "capture" / "content.js").read_text()
    return "const SLOT_CLIMB" + source.split("const SLOT_CLIMB")[1].split("\nfunction esc(")[0]


def test_the_put_chip_refuses_the_autofill_slot_too(browser):
    """The banner's chip picks the input itself, from the words round it -
    and that sentence says "resume" twice, so the parser was both first in
    document order and the best match."""
    async def work(e, page):
        return await page.evaluate(
            "(async () => { window.prompt = () => null;"
            f" {put_source()}\n"
            " const f = new File([new Uint8Array([1])], 'tailored.pdf');"
            " const out = await putFile(f, 'resume');"
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
            await e.call(engine.FIND_REMOVE_FN, engine.COVER_SLOT,
                         f"{engine.RESUME_SLOT}|{engine.PARSE_SLOT}", engine.DELETE_LABELS),
        ]
    resume, cover = on_page(browser, "workday_resume_slot.html", work)
    assert resume and resume["title"] == "Resume/CV" and resume["label"] == "Delete"
    assert "tailored_resume.pdf" in resume["file"]
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
    name = "tailored_resume.pdf"

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
        await engine.mark_named_file(page, "tailored_resume.pdf",
                                     "the tailored resume")
        return await page.evaluate(
            "document.querySelectorAll('[data-autopilot-mark]').length")
    assert on_page(browser, "workday_review.html", work) == 1


def test_a_name_that_is_not_on_the_page_marks_nothing(browser):
    async def work(e, page):
        return await engine.mark_named_file(page, "somebody_elses.pdf", "the tailored resume")
    assert on_page(browser, "workday_review.html", work) is False


# -- the banner at half a screen ------------------------------------------

CONTENT_JS = Path(__file__).resolve().parent.parent / "capture" / "content.js"


def bar_rules() -> str:
    """The three layout rules out of `content.js`, so what is measured here
    is the rule that ships rather than a copy of it. `${c.tone}` and the
    like are the verdict's colour and say nothing about layout."""
    # The interpolations go first: `${c.tone}` carries a `}` of its own, and
    # slicing to the first one cut the `.bar` rule off at its border colour -
    # so the rule under test had no `display: flex` in it at all.
    source = re.sub(r"\$\{[^}]*\}", "#000", CONTENT_JS.read_text())
    out = []
    for name in (".bar {", ".body {", ".actions {"):
        start = source.index("\n      " + name) + 1
        end = source.index("}", start) + 1
        out.append(source[start:end])
    # And the narrow-width rule, which closes a brace later than the others.
    start = source.index("\n      @media (max-width: 560px) {") + 1
    out.append(source[start:source.index("\n      }", start) + 8])
    return "\n".join(out)


def measure_bar(browser, width: int) -> dict:
    async def work(e, page):
        await page.evaluate(
            "(() => { const s = document.createElement('style');"
            f" s.textContent = {json.dumps(bar_rules())};"
            " document.head.appendChild(s); })()")
        await asyncio.sleep(0.2)
        return await page.evaluate(
            "(() => { const r = (s) => document.querySelector(s).getBoundingClientRect();"
            " const body = r('.body'), acts = r('.actions'), bar = r('.bar');"
            " return {bodyW: Math.round(body.width), bodyH: Math.round(body.height),"
            " actsTop: Math.round(acts.top), bodyTop: Math.round(body.top),"
            " actsRight: Math.round(acts.right),"
            " barH: Math.round(bar.height), scrollW: document.documentElement.scrollWidth,"
            " clientW: document.documentElement.clientWidth}; })()")

    async def go(e, page):
        await page.send("Emulation.setDeviceMetricsOverride", {
            "width": width, "height": 700, "deviceScaleFactor": 1, "mobile": False})
        return await work(e, page)
    return on_page(browser, "banner_bar.html", go)


def test_the_banner_summary_is_never_squeezed_into_a_column(browser):
    """In a half-screen window the controls do not shrink - their labels do
    not break - so the verdict was crushed into a column one word wide
    running the height of the page: "No hard or soft flags found" over nine
    lines (2026-10-07, reported with a screenshot)."""
    narrow = measure_bar(browser, 720)
    assert narrow["bodyW"] >= 280, narrow
    # Wrapped, not squeezed: the controls took a row of their own.
    assert narrow["actsTop"] > narrow["bodyTop"], narrow
    assert narrow["scrollW"] <= narrow["clientW"], narrow


def test_the_banner_is_one_row_when_there_is_room(browser):
    wide = measure_bar(browser, 1440)
    assert wide["actsTop"] == wide["bodyTop"], wide
    assert wide["bodyW"] > 280, wide


def test_the_controls_do_not_run_off_a_narrow_window(browser):
    """Narrower than any half of a normal screen. The longest label -
    "Adding to autopilot - click to stop" - is wider than the window by
    then, and a label over two lines beats one that runs off the edge."""
    tiny = measure_bar(browser, 420)
    assert tiny["bodyW"] >= 280, tiny
    assert tiny["actsRight"] <= 420, tiny
    assert tiny["scrollW"] <= tiny["clientW"] + 1, tiny


def test_a_bin_icon_with_no_label_is_still_a_delete(browser):
    """Workday's Resume/CV takes more than one file and each row's control
    is a bin whose only readable name is `title="Delete"`; what always
    names it is `data-automation-id="delete-file"`. Nothing was cleared, so
    the tailored resume went on beside Jobright's: same filename, 60.01 KB
    against 90.48 KB, both "Successfully Uploaded" (2026-10-07, reported
    with a screenshot)."""
    async def work(e, page):
        return [
            await e.call(engine.FIND_REMOVE_FN, engine.RESUME_SLOT,
                         f"{engine.COVER_SLOT}|{engine.PARSE_SLOT}", engine.DELETE_LABELS),
            await e.call(engine.FIND_REMOVE_FN, engine.COVER_SLOT,
                         f"{engine.RESUME_SLOT}|{engine.PARSE_SLOT}", engine.DELETE_LABELS),
        ]
    resume, cover = on_page(browser, "workday_two_resumes.html", work)
    assert resume and resume["title"] == "Resume/CV"
    assert "tailored_resume.pdf" in resume["file"]
    # And it still tells the two slots apart.
    assert cover and cover["title"] == "Cover Letter"
    assert "their_cover_letter.pdf" in cover["file"]


def test_the_label_handed_to_the_guard_is_one_a_reader_would_use(browser):
    """A class name is enough to recognise a control and never enough to
    describe it: the submit guard reads this label before the one click
    this makes on a form."""
    async def work(e, page):
        return await e.call(engine.FIND_REMOVE_FN, engine.RESUME_SLOT,
                            f"{engine.COVER_SLOT}|{engine.PARSE_SLOT}", engine.REMOVE_LABELS)
    found = on_page(browser, "greenhouse_resume_taken.html", work)
    assert found["label"] == "Remove file"

def test_the_slot_is_found_at_the_depth_workday_really_nests_it(browser):
    """The heading that names the slot is nine ancestors above the bin
    button on a live Workday form; the climb stopped at six, found no
    heading at all, cleared nothing, and said so to nobody (2026-10-09,
    reported: two rows on one slot, both 60.38 KB, both the same tailored
    resume). The old fixture put that heading two ancestors up, so the
    suite was green through all of it - this asserts the distance, so a
    shallower fixture cannot make the test pass again."""
    async def work(e, page):
        depth = await page.evaluate("""(() => {
          const btn = document.querySelector('[data-automation-id="delete-file"]');
          let el = btn.parentElement, i = 0;
          while (el && i < 40) {
            i++;
            if ([...el.querySelectorAll("h2,h3,h4,label,legend")]
                  .some((h) => /resume|cv/i.test(h.textContent || ""))) return i;
            el = el.parentElement;
          }
          return -1;
        })()""")
        found = await e.call(engine.FIND_REMOVE_FN, engine.RESUME_SLOT,
                             f"{engine.COVER_SLOT}|{engine.PARSE_SLOT}", engine.DELETE_LABELS)
        return [depth, found]
    depth, found = on_page(browser, "workday_two_resumes.html", work)
    assert depth >= 9, f"the fixture is shallower than the real page ({depth})"
    assert found and found["title"] == "Resume/CV"


def test_every_file_on_a_multi_file_slot_can_be_cleared(browser):
    """Workday keeps one row per file and `remove_attached` clears one, so
    clearing once on a slot holding two left the other exactly where it
    was. `Engine.clear_slot` is the loop; this is the shape it loops over."""
    async def work(e, page):
        rows = []
        for _ in range(4):
            found = await e.call(engine.FIND_REMOVE_FN, engine.RESUME_SLOT,
                                 f"{engine.COVER_SLOT}|{engine.PARSE_SLOT}", engine.DELETE_LABELS)
            if not found:
                break
            rows.append(found["label"])
            # What `CLICK_REMOVE_FN` does, and what the page does after it.
            await page.evaluate("""(() => {
              const btn = document.querySelector("[data-autopilot-remove]");
              btn.closest('[data-automation-id="file-upload-item"]').remove();
            })()""")
        left = await page.evaluate(
            "document.querySelectorAll('[data-automation-id=\"file-upload-item\"]').length")
        return [rows, left]
    rows, left = on_page(browser, "workday_two_resumes.html", work)
    assert len(rows) == 2, rows      # both resume rows, not one
    assert left == 1                 # and the cover letter's row is untouched

def test_the_put_chip_replaces_on_workday_rather_than_adding(browser):
    """Workday keeps the input *and* its rows, and the dropzone takes more
    than one file - so assigning `input.files` added a copy instead of
    replacing one, and three presses of the chip left three copies of the
    same tailored resume on one slot (2026-10-08, reported). Greenhouse and
    Ashby drop the input once a file is on, so this path never had to clear
    anything and never learned how.

    The fixture's bin buttons do not really remove their rows, so the rows
    are taken off here the way Workday takes them off - the thing under test
    is which controls the chip presses, and that it presses them all."""
    async def work(e, page):
        # Workday's own behaviour: the bin takes that row off the list.
        await page.evaluate("""(() => {
          for (const btn of document.querySelectorAll('[data-automation-id="delete-file"]'))
            btn.addEventListener("click", () => setTimeout(() =>
              btn.closest('[data-automation-id="file-upload-item"]').remove(), 50));
        })()""")
        return await page.evaluate(
            "(async () => { window.prompt = () => null;"
            f" {put_source()}\n"
            " const f = new File([new Uint8Array([1])], 'tailored.pdf');"
            " const out = await putFile(f, 'resume');"
            " const rows = [...document.querySelectorAll("
            "   '[data-automation-id=\"file-upload-item-name\"]')].map((d) => d.textContent);"
            " return {out: out, rows: rows}; })()")
    got = on_page(browser, "workday_two_resumes.html", work)
    assert got["out"]["ok"] is True
    # Both copies came off, and the chip says so rather than claiming a put.
    assert "replaced 2 files" in got["out"]["note"], got["out"]["note"]
    # The cover letter's row is not the resume's to clear.
    assert got["rows"] == ["their_cover_letter.pdf"], got["rows"]

def test_the_chip_tells_workdays_two_slots_apart_without_asking(browser):
    """Both of Workday's file inputs sit inside "Drop files here or Select
    files", so the words round them are identical and the chip fell through
    to a prompt offering two lines of the same text - on the one system
    where the resume and the cover letter are separate slots. `slotWords`
    is what tells them apart. The prompt throws here: being asked at all is
    the failure."""
    async def work(e, page):
        return await page.evaluate(
            "(async () => { window.prompt = () => { throw new Error('asked'); };"
            f" {put_source()}\n"
            " const out = {};"
            " for (const key of ['resume', 'cover_letter']) {"
            "   const f = new File([new Uint8Array([1])], key + '.pdf');"
            "   out[key] = await putFile(f, key);"
            " }"
            " out.on = [...document.querySelectorAll('input[type=file]')]"
            "   .map((el) => (el.files && el.files.length) ? el.files[0].name : '');"
            " return out; })()")
    got = on_page(browser, "workday_two_resumes.html", work)
    assert got["resume"]["ok"] is True, got["resume"]
    assert got["cover_letter"]["ok"] is True, got["cover_letter"]
    # Each on its own slot, in document order: resume first, letter second.
    assert got["on"] == ["resume.pdf", "cover_letter.pdf"], got["on"]


# -- Workday, from a recording of a real application ----------------------
# `workday_work_experience.html` and `workday_two_resumes_live.html` are two
# pages of one live Workday application (Vermeer, 2026-10-11), captured by
# `tools/dev_workday_recorder.py` with every value scrubbed. They are here
# because the hand-drawn fixture beside them was green through the whole of
# the bug below: the real page is 31 KB of nesting with two sections that
# each own a control saying "Delete", and that is the shape that broke it.

def test_a_delete_in_another_section_is_not_the_resume_slots(browser):
    """The clear pressed the Work Experience entry's Delete, four times,
    believing it was clearing Resume/CV - and reported `resume replaced`
    over a slot that had never held anything (2026-10-11, Vermeer).

    On this page the two controls reading "Delete" are Work Experience
    entries with no automation id at all, the Resume/CV heading is 17,000
    characters further down the document, and the slot is empty. A climb
    judged by distance reaches from one to the other; a climb that asks
    "is this control inside an upload?" cannot.
    """
    async def work(e, page):
        bare = await page.evaluate("""(() => {
          const all = [...document.querySelectorAll("button")]
              .filter((b) => (b.textContent || "").trim() === "Delete");
          return JSON.stringify({
            count: all.length,
            coded: all.map((b) => b.getAttribute("data-automation-id") || ""),
            attachments: document.querySelectorAll('[data-automation-id="file-upload-item"]').length,
          });
        })()""")
        found = await e.call(engine.FIND_REMOVE_FN, engine.RESUME_SLOT,
                             f"{engine.COVER_SLOT}|{engine.PARSE_SLOT}", engine.REMOVE_LABELS)
        return [json.loads(bare), found]
    bare, found = on_page(browser, "workday_work_experience.html", work)
    # The page really is the trap: bare "Delete" controls, and no attachment.
    assert bare["count"] >= 2, bare
    assert bare["coded"] == [""] * bare["count"], bare
    assert bare["attachments"] == 0, bare
    assert found is None, f"a Work Experience Delete was offered as the resume slot's: {found}"


def test_the_widest_labels_never_match_a_generated_class(browser):
    """`REMOVE_LABELS` carries "change" and "replace" for the systems that
    word it that way, and those were matched against generated class names
    too - so on a live Workday form the phone country selector answered as
    the Resume/CV slot's clearing control and the fill pressed 'United
    States' four times (2026-10-11, Databricks). A coded name may only
    match the narrow list."""
    async def work(e, page):
        found = await e.call(engine.FIND_REMOVE_FN, engine.RESUME_SLOT,
                             f"{engine.COVER_SLOT}|{engine.PARSE_SLOT}", engine.REMOVE_LABELS)
        return found
    found = on_page(browser, "workday_work_experience.html", work)
    assert found is None
    # The rule, not just this page: nothing but remove/delete/clear/x may be
    # read off an identifier, whatever the caller asks for by label.
    source = engine.FIND_REMOVE_FN
    assert "CODED_CLEARS" in source
    assert "coded(btn).some" in source


def test_the_slot_says_how_many_files_it_is_holding(browser):
    """`clear_slot` pressed and counted its own presses, so four failures
    read as four successes and the notes said `resume replaced` over a form
    carrying two resumes. The finder now answers with the row count, which
    is what makes "did that press remove anything?" answerable."""
    async def work(e, page):
        found = await e.call(engine.FIND_REMOVE_FN, engine.RESUME_SLOT,
                             f"{engine.COVER_SLOT}|{engine.PARSE_SLOT}", engine.DELETE_LABELS)
        rows = await e.slot_rows(engine.RESUME_SLOT,
                                 f"{engine.COVER_SLOT}|{engine.PARSE_SLOT}", engine.DELETE_LABELS)
        return [found, rows]
    found, rows = on_page(browser, "workday_two_resumes_live.html", work)
    assert found and found["title"] == "Resume/CV", found
    # The live page carried the same tailored resume twice.
    assert found["rows"] == 2, found
    assert rows == 2


def test_a_press_that_removes_nothing_stops_the_loop_and_says_so(browser):
    """The other half: `clear_slot` keeps going only while the count drops.
    Here nothing is ever removed (the click is swallowed, as Workday's was
    when the control was not its own), so one round happens, the loop stops,
    and the report carries the reason instead of a silent `replaced`."""
    async def work(e, page):
        await page.evaluate("""(() => {
          for (const b of document.querySelectorAll('[data-automation-id="delete-file"]'))
            b.addEventListener("click", (ev) => ev.stopImmediatePropagation(), true);
        })()""")
        cleared = await e.clear_slot(engine.RESUME_SLOT,
                                    f"{engine.COVER_SLOT}|{engine.PARSE_SLOT}",
                                    engine.DELETE_LABELS)
        return [cleared, list(e.report.errors)]
    cleared, errors = on_page(browser, "workday_two_resumes_live.html", work)
    assert cleared == 0, f"nothing came off the slot, so nothing may be reported cleared"
    assert any("changed nothing" in e for e in errors), errors


# -- The selector table, against the pages it was written from ------------
#
# `browser/forms/table.json` is data, and data that has never been run
# against markup is a guess. Every expression the Workday entry carries is
# evaluated here on `workday_two_resumes_live.html` and
# `workday_work_experience.html` - two pages of one real application - and
# on the review fixture, which is the one shape no recording reached.

def on_page_with_rules(cdp: str, name: str, work, documents=None):
    """`on_page`, with a `steps.Runner` over the table's own rules instead
    of the engine. The URL a fixture is served from is a file:// one, so the
    rules are taken by name rather than by host."""
    from browser.forms import steps as step_runner
    from browser.forms import table as form_table

    rules = form_table.load()["workday"]

    async def inner(e, page):
        return await work(step_runner.Runner(page, rules, documents or {}), page)

    return on_page(cdp, name, inner)


def test_the_resume_slot_resolves_on_the_live_workday_page(browser):
    """One element, and it is the one holding the file input - not the
    button that shares its id, and not the skills dropzone beside it."""
    async def work(runner, page):
        rule = runner.rules.field("resume")
        found = [await runner.first(expr) for expr in rule.paths]
        return [(f.ref, f.tag) if f else None for f in found]
    found = on_page_with_rules(browser, "workday_two_resumes_live.html", work)
    assert found[0] is not None, "the resume slot's own path matched nothing"
    assert found[0][1] == "div"


def test_the_slot_knows_how_many_resumes_are_on_it(browser):
    """The page carries two, which is the bug it was recorded for. A count
    is what tells a clear that cleared from a press that did nothing."""
    async def work(runner, page):
        slot = await runner.first(runner.rules.field("resume").paths[0])
        return await runner.slot_rows(slot.ref)
    assert on_page_with_rules(browser, "workday_two_resumes_live.html", work) == 2


def test_the_clearing_control_is_found_inside_the_slot_and_nowhere_else(browser):
    """Scoped to the slot, so there is no fourteen-ancestor climb to reach a
    Work Experience "Delete" or a phone country selector (2026-10-11)."""
    async def work(runner, page):
        slot = await runner.first(runner.rules.field("resume").paths[0])
        path, scope = runner._scoped(runner.rules.slots["clear"], slot.ref)
        inside = await runner.all(path, scope)
        return [(f.tag, f.label or f.text) for f in inside]
    inside = on_page_with_rules(browser, "workday_two_resumes_live.html", work)
    assert len(inside) == 2, inside
    assert all(tag == "button" for tag, _ in inside)


def test_continue_is_found_and_submit_is_not_on_a_form_page(browser):
    async def work(runner, page):
        advance = await runner.first(runner.rules.page("advance"))
        submit = await runner.first(runner.rules.page("submit"))
        return (advance.text if advance else None), (submit.text if submit else None)
    for fixture in ("workday_two_resumes_live.html", "workday_work_experience.html"):
        advance, submit = on_page_with_rules(browser, fixture, work)
        assert advance == "Save and Continue", fixture
        assert submit is None, fixture


def test_on_the_review_page_it_is_submit_and_never_continue(browser):
    """The same `data-automation-id`, the other text. Our walk used to tell
    these apart by asking the guard about the label, which answers yes to
    "Apply" as well - the table's two expressions are the discrimination."""
    async def work(runner, page):
        advance = await runner.first(runner.rules.page("advance"))
        submit = await runner.first(runner.rules.page("submit"))
        return (advance.text if advance else None), (submit.text if submit else None)
    advance, submit = on_page_with_rules(browser, "workday_review_submit.html", work)
    assert advance is None, "the continue expression matched a Submit button"
    assert submit == "Submit"


def test_the_runner_refuses_to_press_the_submit_control(browser):
    """`advance()` is the only press it has, and on the review page it finds
    nothing to press. Were the table edited so that it did, the guard reads
    the label and refuses - which is the half that is not data."""
    async def work(runner, page):
        asked = await runner.advance()
        loosened = dict(runner.rules.pages)
        loosened["advance"] = ".//button[@data-automation-id='pageFooterNextButton']"
        runner.rules = type(runner.rules)(**{**runner.rules.__dict__, "pages": loosened})
        forced = await runner.advance()
        return (asked.done, asked.why), (forced.done, forced.why)
    (asked_done, asked_why), (forced_done, forced_why) = on_page_with_rules(
        browser, "workday_review_submit.html", work)
    assert not asked_done and "no continue button" in asked_why
    assert not forced_done and "reads as a submit" in forced_why


def test_the_review_page_is_recognised_by_its_own_id(browser):
    async def work(runner, page):
        return await runner.at("review"), await runner.at("container")
    review, container = on_page_with_rules(browser, "workday_review_submit.html", work)
    assert review and container
    on_form = on_page_with_rules(browser, "workday_work_experience.html", work)
    assert on_form == (False, True)


def test_the_education_fields_resolve_on_the_page_that_has_them(browser):
    """`workday_work_experience.html` is a real My Experience page: school,
    field of study and grade average are each a `formField-*` block."""
    async def work(runner, page):
        out = {}
        for key in ("school", "field_of_study", "grade_average", "first_name", "email"):
            found = None
            for expr in runner.rules.field(key).paths:
                found = await runner.first(expr)
                if found:
                    break
            out[key] = (found.tag if found else None)
        return out
    out = on_page_with_rules(browser, "workday_work_experience.html", work)
    assert out["school"] == "input" and out["field_of_study"] == "input"
    assert out["grade_average"] == "input"
    # Not on this page, and a field that is not there is not an error.
    assert out["first_name"] is None and out["email"] is None


def test_a_broken_expression_raises_instead_of_reading_as_an_empty_page(browser):
    """A selector that does not compile answering "no fields here" is how a
    broken table would look exactly like a sign-in wall."""
    async def work(runner, page):
        try:
            await runner.all(".//div[")
            return "no error"
        except ValueError as error:
            return str(error)[:40]
    assert on_page_with_rules(browser, "workday_review_submit.html", work) != "no error"


def test_a_text_field_is_written_through_the_native_setter(browser):
    async def work(runner, page):
        rule = runner.rules.field("school")
        out = await runner.run_field(rule, "Example University")
        read = await page.evaluate(
            "document.querySelector('[data-automation-id=\"formField-schoolName\"] input').value")
        return out.done, out.why, read
    done, why, read = on_page_with_rules(browser, "workday_work_experience.html", work)
    assert done, why
    assert read == "Example University"


def test_a_press_that_removes_nothing_stops_the_loop_and_claims_nothing(browser):
    """On a saved page the bin does nothing, because there is no application
    behind it. That is the 2026-10-11 case exactly: the loop must press once,
    see the row count hold still, stop, and report that nothing went - rather
    than press four times and report "resume replaced" over a form carrying
    two resumes."""
    async def work(runner, page):
        await page.evaluate("window.__presses = 0;"
                            "document.addEventListener('click', (e) => {"
                            " if (e.target.closest('[data-automation-id=\"delete-file\"]'))"
                            "   window.__presses += 1; }, true)")
        slot = await runner.first(runner.rules.field("resume").paths[0])
        gone = await runner.clear_slot(slot.ref)
        return gone, await runner.slot_rows(slot.ref), await page.evaluate("window.__presses")
    gone, left, presses = on_page_with_rules(browser, "workday_two_resumes_live.html", work)
    assert gone == 0, "nothing went, so nothing may be reported as having gone"
    assert left == 2
    assert presses == 1, f"pressed {presses} times at a control that changes nothing"


def test_every_file_on_the_dropzone_is_cleared_not_one(browser, tmp_path):
    """The whole sequence where the rows really go: both are cleared, ours
    goes on, and the name is read back off the page rather than taken from
    the upload's own word for it."""
    document = tmp_path / "tailored_resume.pdf"
    document.write_bytes(b"%PDF-1.4\n%%EOF\n")

    async def work(runner, page):
        out = await runner.run_field(runner.rules.field("resume"))
        slot = await runner.first(runner.rules.field("resume").paths[0])
        return (out.done, out.why, out.notes, await runner.slot_rows(slot.ref),
                await page.evaluate("window.__presses"))
    done, why, notes, left, presses = on_page_with_rules(
        browser, "workday_dropzone_rows.html", work, documents={"resume": document})
    assert done, f"{why} {notes}"
    assert presses == 2, "a dropzone keeps a row per file; clearing once leaves the rest"
    assert left == 1, "ours, and only ours"


def test_the_upload_fails_honestly_when_there_is_no_document(browser):
    async def work(runner, page):
        out = await runner.run_field(runner.rules.field("resume"))
        return out.done, out.why
    done, why = on_page_with_rules(browser, "workday_dropzone_rows.html", work)
    assert not done and "no resume to put on" in why


# -- The tracker's own listener, against a page -----------------------------
#
# `tracking/recorder.js` reads somebody else's markup and decides what is a
# secret, what was corrected and why a tab opened. None of those can be
# honestly tested without running it, which is the whole reason this file
# exists.

def tracked(cdp: str, name: str, work):
    """`on_page`, with `tracking/recorder.js` armed and classified the way
    `collector.py` arms and classifies it."""
    import json as _json
    from pathlib import Path as _Path

    from tracking import policy as _policy
    source = (_Path(__file__).resolve().parent.parent / "tracking" / "recorder.js").read_text()

    async def inner(e, page):
        await page.evaluate(source)
        fields = _json.loads(await page.evaluate("window.__trkFields()") or "[]")
        secrets = [f["ref"] for f in fields
                   if _policy.secret_field(f.get("type", ""), f.get("name", ""),
                                           f.get("id", ""), f.get("label", ""),
                                           f.get("autocomplete", ""))]
        await page.evaluate(f"window.__trkClassify({_json.dumps(secrets)}, "
                            f"{_json.dumps([f['ref'] for f in fields])})")
        await page.evaluate("window.__trkDrain()")       # drop the arming row
        return await work(page, fields, secrets)

    return on_page(cdp, name, inner)


async def _drain(page):
    import json as _json
    return _json.loads(await page.evaluate("window.__trkDrain()") or "[]")


def test_the_tracker_knows_which_box_is_the_password(browser):
    async def work(page, fields, secrets):
        return [(f["id"], f["label"], f["ref"] in secrets) for f in fields]
    out = tracked(browser, "tracked_form.html", work)
    assert ("email", "Email Address*", False) in out
    assert ("pw", "Password*", True) in out
    assert ("first", "First Name*", False) in out


def test_what_was_typed_is_kept_and_a_password_is_not(browser):
    async def work(page, fields, secrets):
        await page.evaluate("""(() => {
          const set = (id, v) => { const el = document.getElementById(id);
            el.focus();
            Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(el, v);
            el.dispatchEvent(new Event('input', {bubbles: true})); };
          set('email', 'ada@example.com');
          set('pw', 'hunter2-very-secret');
        })()""")
        return await _drain(page)
    rows = await_rows = tracked(browser, "tracked_form.html", work)
    typed = {r["el"]["id"]: r.get("value") for r in await_rows if r["kind"] == "input"}
    assert typed["email"] == "ada@example.com", "an ordinary value is kept as typed"
    assert typed["pw"] is None, "a password's value may never be recorded"


def test_a_key_in_a_password_box_is_counted_but_never_named(browser):
    async def work(page, fields, secrets):
        await page.evaluate("""(() => {
          const hit = (id, key) => document.getElementById(id)
              .dispatchEvent(new KeyboardEvent('keydown', {key: key, bubbles: true}));
          hit('first', 'A'); hit('pw', 'h'); hit('pw', 'Backspace');
        })()""")
        return await _drain(page)
    keys = [(r["el"]["id"], r["key"]) for r in tracked(browser, "tracked_form.html", work)
            if r["kind"] == "key"]
    assert ("first", "A") in keys
    assert ("pw", "<secret>") in keys and ("pw", "h") not in keys
    assert sum(1 for i, _ in keys if i == "pw") == 2, "the keystrokes still count"


def test_a_correction_of_an_autofill_is_told_from_ones_own_typing(browser):
    """The row the whole tracker exists for: what a script put there, what it
    was left as, and whether that was a correction.

    The person's half has to go through CDP. An event a script dispatches is
    `isTrusted: false` however it is made, and that flag is the only thing
    that tells a hand from an extension - so a test that types with
    `dispatchEvent` is testing the script path twice.
    """
    async def work(page, fields, secrets):
        ref = [f for f in fields if f["id"] == "first"][0]["ref"]
        await page.evaluate("""(() => {
          const el = document.getElementById('first');
          el.focus();
          Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')
              .set.call(el, 'Adaa');                       // a script's value
          el.dispatchEvent(new Event('input', {bubbles: true}));
          el.setSelectionRange(3, 4);                      // select the stray 'a'
        })()""")
        await page.send("Input.insertText", {"text": ""})   # a real deletion
        await page.evaluate("document.getElementById('first').blur()")
        return ref, await _drain(page)
    ref, rows = tracked(browser, "tracked_form.html", work)
    mine = [r for r in rows if r["kind"] == "input" and r.get("trusted")]
    assert mine, "CDP input must arrive trusted, or this tests nothing"
    edits = [r for r in rows if r["kind"] == "edit"]
    assert len(edits) == 1, rows
    edit = edits[0]
    assert edit["was"] == "" and edit["now"] == "Ada"
    assert edit["filled"] == "Adaa", "what the script had put there is kept"
    assert edit["corrected"] is True


def test_an_error_is_recorded_when_it_appears_and_again_when_it_clears(browser):
    async def work(page, fields, secrets):
        import asyncio as _a
        await page.evaluate("document.getElementById('complaint').style.display = 'block'")
        await _a.sleep(1.2)
        first = await _drain(page)
        await page.evaluate("document.getElementById('complaint').style.display = 'none'")
        await _a.sleep(1.2)
        return first, await _drain(page)
    appeared, cleared = tracked(browser, "tracked_form.html", work)
    raised = [r for r in appeared if r["kind"] == "error"]
    gone = [r for r in cleared if r["kind"] == "error"]
    assert raised and raised[0]["what"] == "raised"
    assert "First Name is required" in raised[0]["says"]
    assert gone and gone[0]["what"] == "cleared"


def test_a_blank_tab_carries_the_click_that_opened_it(browser):
    """Three things can open a blank tab on a Workday page and from the
    outside they are identical. The patched `window.open` records the call's
    own stack and the last control the person touched."""
    async def work(page, fields, secrets):
        await page.evaluate("""(() => {
          const b = document.getElementById('opener');
          b.dispatchEvent(new PointerEvent('pointerdown', {bubbles: true}));
          b.click();
        })()""")
        return await _drain(page)
    rows = tracked(browser, "tracked_form.html", work)
    opens = [r for r in rows if r["kind"] == "window.open"]
    assert len(opens) == 1, rows
    asked = opens[0]
    assert asked["name"] == "_blank" and asked["wanted"] == ""
    touched = (asked.get("after") or {}).get("el") or {}
    assert touched.get("says") == "Autofill for Another Job"
    assert asked["sinceTouch"] is not None and asked["sinceTouch"] < 2000
    assert asked["from"], "the call's own stack is what names a script"


def test_a_tab_that_closes_itself_says_so(browser):
    """The difference between "I close these tabs" and "something closes
    them": one leaves a `window.close` row with a stack, the other leaves
    nothing in the page at all.

    `window.close` is replaced with a no-op *before* the recorder wraps it,
    because the recorder calls through to the real one and a tab that really
    closes takes the evidence with it.
    """
    import json as _json
    from pathlib import Path as _Path

    from tracking import policy as _policy
    source = (_Path(__file__).resolve().parent.parent / "tracking" / "recorder.js").read_text()

    async def inner(e, page):
        await page.evaluate("window.close = function () { window.__closed = true; };")
        await page.evaluate(source)
        fields = _json.loads(await page.evaluate("window.__trkFields()") or "[]")
        await page.evaluate(f"window.__trkClassify([], {_json.dumps([f['ref'] for f in fields])})")
        await page.evaluate("window.__trkDrain()")
        await page.evaluate("""(() => {
          const b = document.getElementById('closer');
          b.dispatchEvent(new PointerEvent('pointerdown', {bubbles: true}));
          b.click();
        })()""")
        return _json.loads(await page.evaluate("window.__trkDrain()") or "[]"), \
            await page.evaluate("!!window.__closed")

    rows, called = on_page(browser, "tracked_form.html", inner)
    assert called, "the wrapper must call through to the real window.close"
    closes = [r for r in rows if r["kind"] == "window.close"]
    assert len(closes) == 1
    assert ((closes[0].get("after") or {}).get("el") or {}).get("says") == "Close me"
    assert closes[0]["from"], "the stack is what names the caller"


def test_the_listener_refuses_to_arm_on_an_off_limits_page(browser):
    """The guard is in the injected source, not in the poll loop: a tab that
    walks from a careers page into webmail must go quiet by itself. Asked of
    the predicate, since serving a page from mail.google.com is not
    something a test can do."""
    from tracking import collector

    async def work(e, page):
        await page.evaluate(collector._armed())
        return await page.evaluate("""JSON.stringify([
          window.__trkAllowed("acme.wd1.myworkdayjobs.com"),
          window.__trkAllowed("mail.google.com"),
          window.__trkAllowed("a.b.mail.google.com"),
          window.__trkAllowed("notchase.com"),
          window.__trkAllowed("www.chase.com"),
          typeof window.__trkDrain,
        ])""")
    import json as _json
    workday, mail, deep, lookalike, bank, armed = _json.loads(
        on_page(browser, "tracked_form.html", work))
    assert workday and lookalike, "an ordinary host is read"
    assert not mail and not deep and not bank, "mail and banking are never read"
    assert armed == "function", "a local fixture is not off limits, so it arms"


# -- the put chip: a caption above a slot is not the slot's own identity ---

def test_a_caption_above_the_panel_does_not_disqualify_the_real_slot(browser):
    """Workday's own offer is captioned "Autofill with Resume", and on a
    tenant that puts it at the head of the panel holding the Resume/CV
    dropzone, `slotWords` climbs past the dropzone - which names itself
    nothing - to that heading and hands it back as the slot's name. The put
    chip then read the system's own parser hint on the one control that is
    not one and refused the page.

    The rescue reads the control's own identity and the block beside it, and
    the test that it is a rescue rather than a hole is the next one down:
    Ashby's real parser still refuses, on its own live markup.
    """
    async def work(e, page):
        return await page.evaluate(
            "(async () => { window.prompt = () => { throw new Error('asked'); };"
            f" {put_source()}\n"
            " const f = new File([new Uint8Array([1])], 'tailored_resume.pdf');"
            " const out = await putFile(f, 'resume');"
            " const on = [...document.querySelectorAll('input[type=file]')]"
            "   .map((el) => (el.files && el.files.length) ? el.files[0].name : '');"
            " return {out: out, on: on}; })()")
    got = on_page(browser, "workday_autofill_caption.html", work)
    assert got["out"]["ok"] is True, got["out"]
    assert got["on"] == ["tailored_resume.pdf"], got["on"]


def test_the_rescue_still_refuses_ashbys_own_parser(browser):
    """The other half. On the second pass Ashby has dropped the real slot's
    input and its parser is the only file input left - and that parser
    describes itself in the block the input sits in ("Autofill from resume.
    Upload your resume here to autofill key application fields."), which is
    what the control's own identity means. A rescue that read it as a slot
    would put the tailored resume into the thing that parses it over the
    fields instead of attaching it."""
    async def work(e, page):
        return await page.evaluate(
            "(async () => { window.prompt = () => null;"
            f" {put_source()}\n"
            " const f = new File([new Uint8Array([1])], 'tailored_resume.pdf');"
            " const out = await putFile(f, 'resume');"
            " const on = [...document.querySelectorAll('input[type=file]')]"
            "   .map((el) => (el.files && el.files.length) ? el.files[0].name : '');"
            " return {out: out, on: on}; })()")
    got = on_page(browser, "ashby_resume_taken.html", work)
    assert got["out"]["ok"] is False
    assert "not the resume slot" in got["out"]["note"]
    assert got["on"] == [""], "the resume went onto the parser"


def test_the_real_workday_dropzone_is_never_read_as_a_parser(browser):
    """And the shape that is on disk from a real page: the concern was that
    Workday's caption reaches the dropzone's own name and filters it out. It
    does not - the climb stops at `resumeAttachments--attachments`, which is
    the slot naming itself - and this pins that, so the rescue above is never
    asked to do the work that the ordinary path already does."""
    async def work(e, page):
        return await page.evaluate(
            "(async () => { window.prompt = () => { throw new Error('asked'); };"
            f" {put_source()}\n"
            " const f = new File([new Uint8Array([1])], 'tailored_resume.pdf');"
            " const out = await putFile(f, 'resume');"
            " const on = document.querySelector('input[type=file]').files[0].name;"
            " return {out: out, on: on}; })()")
    got = on_page(browser, "workday_dropzone_rows.html", work)
    assert got["out"]["ok"] is True, got["out"]
    # Found without asking (the prompt throws) and without the rescue: the
    # slot named itself, so the caption never came into it.
    assert got["on"] == "tailored_resume.pdf", got["on"]


# -- the banner's file chips, run in a browser -----------------------------

def html_sink_source() -> str:
    """`trustedHTML` and `setHTML`, which every piece of markup in
    `content.js` goes through since a Trusted Types page refused the raw
    sink (2026-11-03). Spliced in wherever a function is lifted out of the
    file on its own: without it the lifted code is missing the one call it
    makes to render anything."""
    source = CONTENT_JS.read_text()
    start = source.index("let htmlPolicy;")
    end = source.index("const LABELS = {")
    return source[start:end]


def files_source() -> str:
    """`loadFiles` as it ships: the chips, the context line, the Finder
    button and the drag handler. Taken with `put_source()` because the chips
    call `putFile`."""
    source = CONTENT_JS.read_text()
    start = source.index("async function loadFiles(")
    end = source.index("// Takes whatever is already on a slot")
    return source[start:end]


# What the server's `/review/<id>/files` answers with, as the chips expect
# it: the upload name, the type, and the bytes in base64 because the page's
# bridge carries json and not bytes.
FILES_STUB = """
    const esc = (s) => String(s).replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[ch]));
    const post = async (path, body) => ({
      ok: true, status: 200, json: async () => (path.endsWith("/files") ? {
        resume: {name: "tailored_resume.pdf", type: "application/pdf", b64: btoa("%PDF-1.4 resume")},
        cover_letter: {name: "cover_letter.pdf", type: "application/pdf", b64: btoa("%PDF-1.4 letter")},
      } : {opened: true, revealed: ["tailored_resume.pdf"]})});
    const shadow = document.createElement("div");
    shadow.attachShadow({mode: "open"}).innerHTML = '<div class="files" id="files"></div>';
    shadow.shadowRoot.getElementById = (id) => shadow.shadowRoot.querySelector("#" + id);
    document.body.appendChild(shadow);
"""


def test_the_chip_carries_the_real_pdf_bytes_into_the_drag(browser):
    """The chip is `draggable` and its `dragstart` puts the document into the
    DataTransfer as a `File`, so a drop zone or an `<input type=file>` takes
    it as it would a file from the desktop. What is measured here is that the
    handler really loads the bytes the server sent - not a name, not a URL -
    because a drag that carries only text looks identical until the drop.
    """
    async def work(e, page):
        return await page.evaluate(
            "(async () => {"
            f" {html_sink_source()}\n{FILES_STUB}\n{put_source()}\n{files_source()}\n"
            " await loadFiles(shadow.shadowRoot, 'abcd', 'filled');"
            " const row = shadow.shadowRoot.getElementById('files');"
            " const chip = row.querySelector('.chip[data-key=resume]');"
            " const dt = new DataTransfer();"
            " chip.dispatchEvent(new DragEvent('dragstart', {dataTransfer: dt, bubbles: true}));"
            " const text = await (dt.files[0] ? dt.files[0].text() : Promise.resolve(''));"
            " return {draggable: chip.getAttribute('draggable'),"
            "   count: dt.files.length, name: dt.files[0] && dt.files[0].name,"
            "   type: dt.files[0] && dt.files[0].type, body: text,"
            "   download: dt.getData('DownloadURL'), plain: dt.getData('text/plain'),"
            "   what: row.querySelector('.what').textContent,"
            "   finder: !!row.querySelector('button.finder')}; })()")
    got = on_page(browser, "workday_dropzone_rows.html", work)
    assert got["draggable"] == "true"
    assert got["count"] == 1 and got["name"] == "tailored_resume.pdf"
    assert got["type"] == "application/pdf"
    assert got["body"] == "%PDF-1.4 resume", "the drag carried a name, not the document"
    # And the two fallbacks for a target that reads neither: Chrome's own
    # download channel, and the filename as plain text.
    assert got["download"].startswith("application/pdf:tailored_resume.pdf:")
    assert got["plain"] == "tailored_resume.pdf"
    # The context a person checking a filled form wants, without opening the
    # review page: which resume and which letter went on this job.
    assert "tailored_resume.pdf" in got["what"] and "cover_letter.pdf" in got["what"]
    assert got["finder"], "no way back to the files in Finder"


def test_a_drop_of_that_drag_lands_a_real_file_on_a_slot(browser):
    """The other end of it: the DataTransfer the chip filled is what a drop
    target reads, so a form's own dropzone gets a `File` with the bytes in
    it. This is the in-page drop, which is what the ATS dropzones are; a
    native drop target outside the browser is a different question and is
    answered by the Finder button beside the chips."""
    async def work(e, page):
        return await page.evaluate(
            "(async () => {"
            f" {html_sink_source()}\n{FILES_STUB}\n{put_source()}\n{files_source()}\n"
            " await loadFiles(shadow.shadowRoot, 'abcd', 'filled');"
            " const chip = shadow.shadowRoot.querySelector('.chip[data-key=resume]');"
            " const dt = new DataTransfer();"
            " chip.dispatchEvent(new DragEvent('dragstart', {dataTransfer: dt, bubbles: true}));"
            " const zone = document.querySelector('.dropzone');"
            " let seen = null;"
            " zone.addEventListener('drop', (ev) => {"
            "   seen = [...ev.dataTransfer.files].map((f) => f.name); });"
            " zone.dispatchEvent(new DragEvent('drop', {dataTransfer: dt, bubbles: true}));"
            " return seen; })()")
    assert on_page(browser, "workday_dropzone_rows.html", work) == ["tailored_resume.pdf"]


# -- Start now, and the question thread ------------------------------------

def start_source() -> str:
    """`startNow`, `askThread` and `revealFiles` as they ship, from the first
    through the end of the third."""
    source = CONTENT_JS.read_text()
    start = source.index("async function startNow(")
    end = source.index("// The tailored PDFs for this job, as chips.")
    return source[start:end]


def drive(browser, script: str):
    """The three functions above with the state they close over and a `post`
    the test answers, so what is measured is the shipped handling of the
    server's own contract."""
    async def work(e, page):
        return await page.evaluate(
            "(async () => {"
            " const START = {note: '', candidates: [], busy: false, state: ''};"
            " const ASK = {jobId: null, turns: [], busy: false, note: '', open: true, draft: ''};"
            " let jobHint = null;"
            " const paintStart = () => {}; const paintAsk = () => {};"
            " let sent = [];"
            " let reply = {ok: true, status: 200, body: {}};"
            " const post = async (path, body) => { sent.push([path, body]);"
            "   return {ok: reply.ok, status: reply.status, json: async () => reply.body}; };"
            f" {start_source()}\n"
            f" {script}\n"
            " })()")
    return on_page(browser, "workday_dropzone_rows.html", work)


def test_a_fill_already_running_is_joined_and_said_so(browser):
    """`synced` is the one answer that has to be spoken: a fill was already
    running for this job and this press joined it rather than starting a
    second. Unsaid, a second press reads as the first having failed - and
    two fills on one tab is the defect the one-at-a-time rule exists for."""
    got = drive(browser, """
      reply = {ok: true, status: 200, body:
        {started: false, state: "synced", job_id: "ab12", note: ""}};
      await startNow(null);
      return {state: START.state, note: START.note, picks: START.candidates.length,
              hint: jobHint && jobHint.id, sent: sent};
    """)
    assert got["state"] == "synced"
    assert "already running" in got["note"].lower() and "joined" in got["note"].lower()
    assert got["picks"] == 0
    assert got["hint"] == "ab12", "the job it joined is this page's job from now on"
    assert got["sent"] == [["/start-now", {"url": got["sent"][0][1]["url"]}]]


def test_a_page_that_is_not_a_job_offers_the_jobs_that_are(browser):
    """The YC case: the URL on file is the listing, the person is on the
    employer's Workday form, and the identity ladder correctly says these
    are not the same page. Before, that meant no chips, no Finder button and
    no drag - the tailored resume was never offered. Now the in-flight jobs
    come back and the person says which one it is."""
    got = drive(browser, """
      reply = {ok: true, status: 200, body: {started: false, state: "no_job", job_id: null,
        note: "", candidates: [{id: "da8b", title: "Physical AI Infra", company: "Yondu"}]}};
      await startNow(null);
      return {state: START.state, note: START.note, picks: START.candidates,
              hint: jobHint};
    """)
    assert got["state"] == "no_job"
    assert got["picks"] == [{"id": "da8b", "title": "Physical AI Infra", "company": "Yondu"}]
    assert "not a job in autopilot" in got["note"]
    assert got["hint"] is None, "a candidate list is not a choice"


def test_a_picked_job_is_started_in_this_very_tab(browser):
    """With the job named, the server runs the fill in **this** tab instead
    of opening one on the job's recorded URL - which for a job captured off
    a listing site is the listing, not the form the person is looking at."""
    got = drive(browser, """
      reply = {ok: true, status: 200, body: {started: true, state: "started",
        job_id: "da8b", note: "Filling this form."}};
      await startNow("da8b");
      return {sent: sent, state: START.state, note: START.note};
    """)
    assert got["sent"][0][0] == "/start-now"
    assert got["sent"][0][1]["job_id"] == "da8b"
    assert got["sent"][0][1]["url"].startswith("file://"), got["sent"]
    assert got["state"] == "started" and got["note"] == "Filling this form."


def test_a_visa_question_comes_back_refused_and_is_shown_as_the_answer(browser):
    """The refusal lives server-side (`guard.ProtectedField` -> 422) and it
    is the answer, not an error to retry: that one is the person's to answer
    and nothing here may work around it. It lands in the thread, in red."""
    got = drive(browser, """
      ASK.draft = "Will you now or in the future require sponsorship?";
      reply = {ok: false, status: 422, body:
        {detail: "That is a visa / work-authorisation question; it is yours to answer."}};
      await askThread({id: "ab12"});
      return {kinds: ASK.turns.map((t) => t.kind), texts: ASK.turns.map((t) => t.text),
              busy: ASK.busy, sent: sent};
    """)
    assert got["kinds"] == ["question", "error"]
    assert "yours to answer" in got["texts"][1]
    assert got["busy"] is False
    # The job's own thread, not a second endpoint of our own.
    assert got["sent"][0][0] == "/review/ab12/thread"
    assert got["sent"][0][1]["action"] == "ask"


def test_the_thread_keeps_the_turns_the_server_sends_back(browser):
    """A thread, not a one-shot: the reply carries every turn of the job's
    conversation, so the transcript is the server's and the next question
    has the last answer above it."""
    got = drive(browser, """
      ASK.draft = "Why do you want to work here?";
      reply = {ok: true, status: 200, body: {turns: [
        {kind: "question", text: "Why do you want to work here?"},
        {kind: "answer", text: "Because of the inference work.", model: "cheap/model"}]}};
      await askThread({id: "ab12"});
      const before = ASK.draft;
      return {kinds: ASK.turns.map((t) => t.kind), draft: before, note: ASK.note};
    """)
    assert got["kinds"] == ["question", "answer"]
    assert got["draft"] == "", "the box is cleared once the question is sent"
    assert got["note"] == ""


def test_an_empty_question_is_not_sent(browser):
    """A button that needs text says so rather than doing nothing: three
    clicks on an empty box with no message is how the review page's own
    Re-tailor read as broken (2026-09-24)."""
    got = drive(browser, """
      ASK.draft = "   ";
      await askThread({id: "ab12"});
      return {sent: sent.length, note: ASK.note, turns: ASK.turns.length};
    """)
    assert got["sent"] == 0 and got["turns"] == 0
    assert got["note"] == "Type the question first."


# -- Add to autopilot, in Jobright's own button row ------------------------

def jr_add_source() -> str:
    """The button, from `const JR_ADD_ID` through the end of
    `fitJobrightAdd`: the constants, the builder and the fit."""
    source = CONTENT_JS.read_text()
    start = source.index('const JR_ADD_ID = ')
    end = source.index("function mountJobrightAdd()")
    return source[start:end]


def apply_control_source() -> str:
    """`applyControl`, which both the big button and the press agree on."""
    source = CONTENT_JS.read_text()
    start = source.index("const APPLY_LABEL = /^")
    return source[start:source.index("\n// A real press,")]


def place_jr_add(browser, width: int) -> dict:
    async def work(e, page):
        await page.send("Emulation.setDeviceMetricsOverride", {
            "width": width, "height": 800, "deviceScaleFactor": 1, "mobile": False})
        return await page.evaluate(
            "(() => {"
            f" {html_sink_source()}\n{apply_control_source()}\n{jr_add_source()}\n"
            " const row = document.querySelector('.row');"
            " const before = {scroll: row.scrollWidth, client: row.clientWidth,"
            "   kids: row.children.length};"
            " const apply = applyControl();"
            " const built = buildJobrightAdd();"
            " fitJobrightAdd(built.host, apply, built.root.querySelector('button.go'));"
            " const host = document.getElementById('job-autopilot-jr-add');"
            " const go = built.root.querySelector('button.go');"
            " const box = go.getBoundingClientRect(), ab = apply.getBoundingClientRect();"
            " return {before: before, after: {scroll: row.scrollWidth, client: row.clientWidth},"
            "   inRow: host.parentElement === row, afterApply: apply.nextElementSibling === host,"
            "   label: go.textContent, fixed: getComputedStyle(host).position,"
            "   height: Math.round(box.height), applyHeight: Math.round(ab.height),"
            "   radius: getComputedStyle(go).borderRadius,"
            "   applyRadius: getComputedStyle(apply).borderRadius,"
            "   docScroll: document.documentElement.scrollWidth,"
            "   rowMin: getComputedStyle(document.querySelector('main')).minWidth}; })()")
    return on_page(browser, "jobright_apply_row.html", work)


def test_the_add_button_is_a_sibling_of_their_apply_and_matches_it(browser):
    """Their size, their row: the height and the radius are taken from Apply
    itself rather than guessed, so the two read as one row. Only the colour
    is ours, which is how you tell whose button it is."""
    got = place_jr_add(browser, 1440)
    assert got["inRow"] and got["afterApply"]
    assert got["fixed"] == "inline-flex" or got["fixed"] == "static", got["fixed"]
    assert got["height"] == got["applyHeight"], got
    assert got["radius"] == got["applyRadius"], got
    assert got["label"] == "Add to autopilot"


def test_the_add_button_never_pushes_their_layout_sideways(browser):
    """Jobright hard-codes `min-width: 1200px` on its main content, and the
    injector overrides it so the page reads at half a screen - which is where
    this button usually lives. There it has to give way rather than push:
    the label shortens, and if the row still overflows the button leaves the
    row and floats under the badge. Measured against the row's own box
    before and after, at a full screen and at half of one."""
    for width in (1440, 720, 420):
        got = place_jr_add(browser, width)
        grew = got["after"]["scroll"] - got["before"]["scroll"]
        fits = got["after"]["scroll"] <= got["after"]["client"] + 1
        if got["fixed"] == "fixed":
            # It gave up the row entirely, which is the last resort.
            assert grew <= 0, (width, got)
        else:
            assert fits or got["before"]["scroll"] > got["before"]["client"], (width, got)
            assert got["label"] in ("Add to autopilot", "Add"), (width, got)
        # And their own minimum is never written over by this: that is the
        # injector's decision for the split, not this button's.
        assert got["rowMin"] == "1200px", (width, got)


# -- the whole banner, injected and answered -------------------------------

# The bridge the injector binds (`browser/inject.py`): the page hands a
# json request to `__autopilotRequest` and the reply comes back through
# `__autopilotReply`. Answering it here is what lets the whole of
# `content.js` run in a browser - and the point of running the whole of it
# is that `/capture` is a call the test can watch for and never see.
BRIDGE = """
  window.__calls = [];
  window.__autopilotRequest = (raw) => {
    const req = JSON.parse(raw);
    window.__calls.push(req.path);
    const body = req.path === "/screen" ? window.__screenReply
      : req.path.endsWith("/files") ? {
          resume: {name: "tailored_resume.pdf", type: "application/pdf", b64: btoa("%PDF resume")},
          cover_letter: {name: "cover_letter.pdf", type: "application/pdf", b64: btoa("%PDF letter")}}
      : {};
    setTimeout(() => window.__autopilotReply(req.id, true, 200, body), 0);
  };
"""


# A closed shadow root cannot be read from outside, so the probe runs inside
# the page with the root handed to it by patching attachShadow before the
# script is injected. That patch is the test's, not the shipping code's.
OPEN_ROOTS = """
  const realAttach = Element.prototype.attachShadow;
  Element.prototype.attachShadow = function (init) {
    const root = realAttach.call(this, {...init, mode: "open"});
    return root;
  };
"""


def banner(browser, screen_reply: dict, wait: float, probe: str):
    async def work(e, page):
        await page.evaluate(f"(() => {{ {OPEN_ROOTS}\n{BRIDGE}\n"
                            f" window.__screenReply = {json.dumps(screen_reply)}; }})()")
        await page.evaluate(CONTENT_JS.read_text())
        await asyncio.sleep(wait)
        return await page.evaluate(
            "(() => { const host = document.getElementById('job-autopilot-screen');"
            " const r = host.shadowRoot;"
            f" {probe} }})()")
    return on_page(browser, "banner_live.html", work)


CLEAN = {"verdict": "ok", "summary": "No hard or soft flags found",
         "facts_source": "profile", "flags": []}


def test_a_clean_verdict_queues_nothing_and_presses_nothing(browser):
    """The whole of the 2026-10-30 change, measured rather than read off the
    source: a clean verdict on a page nobody has pointed at a job used to
    call `/capture` two seconds later, and on Jobright's own posting page to
    press their Apply. It queued `login.usc.edu/login` twice, a SAP page
    titled "Career Opportunities: Sign In" and a bare careers homepage.

    The banner is given four seconds - twice the old countdown - and the
    only call it may have made is the screen itself.
    """
    got = banner(browser, CLEAN, 4.0,
                 " return {calls: window.__calls,"
                 "   add: r.querySelector('button.add') && r.querySelector('button.add').textContent,"
                 "   counting: !!r.querySelector('.counting'),"
                 "   verdict: r.querySelector('.tag').textContent.trim(),"
                 "   summary: r.querySelector('.summary').textContent};")
    assert got["calls"] == ["/screen"], got["calls"]
    assert got["add"] == "Add to autopilot", got["add"]
    assert got["counting"] is False, "something is still counting down"
    # And the verdict is still screened and still shown: it is information,
    # and only acting on it by itself was the problem.
    assert got["verdict"] == "OK"
    assert got["summary"] == "No hard or soft flags found"


def test_start_now_waits_under_the_orb_until_it_is_pressed(browser):
    """"It has to stop. It should not automatically start playing." The bar
    says the verdict and folds into the badge after three seconds; what is
    left on the screen is the orb and, under it, Start now - the one control
    that hands the form to the autopilot, and only when pressed."""
    # The screen settles at 1.5 s and the bar folds 3 s after the verdict is
    # painted, so six seconds is the first moment the badge is the whole of
    # what is on the screen.
    got = banner(browser, CLEAN, 6.0,
                 " const s = r.querySelector('.startnow');"
                 " const b = s.getBoundingClientRect();"
                 " const badge = r.querySelector('button.badge').getBoundingClientRect();"
                 " return {calls: window.__calls, off: s.classList.contains('off'),"
                 "   label: r.querySelector('button.start').textContent,"
                 "   collapsed: host.dataset.collapsed,"
                 "   below: Math.round(b.top) >= Math.round(badge.bottom),"
                 "   left: Math.round(b.left), shown: b.height > 0};")
    assert got["collapsed"] == "true", "the bar did not fold into the badge"
    assert got["off"] is False and got["shown"] is True
    assert got["label"] == "Start now"
    assert got["below"], "Start now is over the orb, not under it"
    assert got["left"] <= 12
    # Drawing it costs nothing: still only the screen has been called.
    assert got["calls"] == ["/screen"]


def test_a_matched_job_brings_its_files_its_names_and_its_thread(browser):
    """A page the screen matched to a job in autopilot carries that job's
    own context: which resume and letter went on it, the chips to drag or
    put, the way back to them in Finder, and somewhere to ask about the
    form's own questions. All of it used to mean opening the review page."""
    reply = dict(CLEAN, seen={"id": "ab12", "status": "approved", "level": "high",
                              "company": "Yondu", "title": "Infra Engineer",
                              "at": "2026-10-29T10:00:00+00:00"})
    got = banner(browser, reply, 4.0,
                 " const files = r.getElementById('files');"
                 " return {calls: window.__calls,"
                 "   what: files.querySelector('.what').textContent,"
                 "   chips: [...files.querySelectorAll('.chip')].length,"
                 "   finder: !!files.querySelector('button.finder'),"
                 "   ask: !!r.querySelector('[data-act=ask-open]'),"
                 "   start: !!r.querySelector('button.start')};")
    assert "/review/ab12/files" in got["calls"]
    assert "tailored_resume.pdf" in got["what"] and "cover_letter.pdf" in got["what"]
    assert got["chips"] == 4, "two files, each with a drag chip and a put chip"
    assert got["finder"] and got["ask"] and got["start"]
    # Still nothing queued and nothing started.
    assert "/capture" not in got["calls"] and "/start-now" not in got["calls"]
