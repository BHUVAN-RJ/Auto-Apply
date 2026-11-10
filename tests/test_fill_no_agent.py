"""The fill with the browser model switched off, which is how it really runs.

`tests/test_fill.py` is the home of the fill's tests and `scripts/check.sh`
skips the whole file, because it imports browser-use and the app no longer
installs it. `browser/fill.py` itself imports browser-use inside the function
that needs it, so the no-model ending can be tested without it - and it had
to be, because that ending raised `NameError` on every run for as long as
`AUTOPILOT_AGENT=0` has been set and no test that runs ever reached it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from browser import autofill, fill
from browser.forms.engine import Report


@pytest.fixture
def quiet(monkeypatch, tmp_path):
    """The three things the ending does to the outside world, recorded."""
    wrote: dict = {}
    told: list = []

    async def shot(cdp_url, target_id, path):
        return path / "form.png"

    async def report(filled, path, cdp_url, url, target_id):
        wrote.update(path=path, url=url, target_id=target_id)

    async def notify(cdp_url, target_id, state, message):
        told.append((state, message))

    monkeypatch.setattr(fill, "_screenshot_cdp", shot)
    monkeypatch.setattr(fill, "_write_report", report)
    monkeypatch.setattr(autofill, "notify", notify)
    return wrote, told


def test_the_no_model_ending_reports_the_page_the_form_was_on(quiet, tmp_path):
    """The regression: `form_url` was read off the caller's scope by accident.

    The name exists in `run()`, so nothing complained until the branch ran,
    and then it raised `NameError: name 'form_url' is not defined` *after*
    the resume and the cover letter were already on the form. The tab was
    perfect and the job was marked failed.
    """
    wrote, told = quiet
    filled = Report()
    filled.resume_uploaded = True
    filled.cover_letter_uploaded = True

    result = asyncio.run(fill._finish_without_agent(
        "http://127.0.0.1:9333",
        "https://job-boards.greenhouse.io/acme/jobs/1#app",
        "TARGET-1",
        filled,
        autofill.AutofillResult(),
        tmp_path,
        True,
    ))

    assert result.ok is True
    assert result.steps == 0
    # The form's own page, not the posting it was captured from.
    assert wrote["url"] == "https://job-boards.greenhouse.io/acme/jobs/1#app"
    assert told and told[0][0] == "done"
    assert "no browser model ran on this form" in result.notes


def test_a_resume_that_did_not_attach_is_not_a_filled_form(quiet, tmp_path):
    """`ok` is the resume being on the form, never the run having finished."""
    wrote, told = quiet
    result = asyncio.run(fill._finish_without_agent(
        "http://127.0.0.1:9333", "https://example.com/apply", "TARGET-2",
        Report(), autofill.AutofillResult(), tmp_path, False,
    ))
    assert result.ok is False
    assert any("never uploaded" in line for line in result.errors)
    assert told and told[0][0] == "error"
