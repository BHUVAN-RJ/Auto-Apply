"""The table itself: does it load, is every entry usable, and does the one
rule that matters survive an edit.

Whether an expression selects what it means to is a question about markup
and is answered in `tests/test_dom.py`, against the real pages. This file is
about the shape.
"""
from __future__ import annotations

import json

import pytest

from browser.forms import table


def test_every_entry_in_the_shipped_table_is_usable():
    problems = list(table.problems())
    assert problems == [], "; ".join(f"{name}: {why}" for name, why in problems)


def test_the_table_is_read_at_call_time(tmp_path):
    """A module-level `Path` default freezes at import, which this file has
    been bitten by twice. `load` resolves its own, so a test can point it
    somewhere else."""
    other = tmp_path / "table.json"
    other.write_text(json.dumps({"systems": {"acme": {"hosts": ["acme.test"]}}}))
    assert list(table.load(other)) == ["acme"]
    assert "workday" in table.load()


@pytest.mark.parametrize("url,expected", [
    ("https://acme.wd1.myworkdayjobs.com/en-US/careers/job/x", "workday"),
    ("https://ACME.WD5.MYWORKDAYJOBS.COM/careers/job/x", "workday"),
    ("https://acme.wd1.myworkdaysite.com/careers", "workday"),
    ("https://job-boards.greenhouse.io/acme/jobs/1", None),
    ("https://notmyworkdayjobs.com.evil.test/x", None),
    ("", None),
    ("not a url", None),
])
def test_a_url_finds_its_system_by_host_suffix_only(url, expected):
    rules = table.rules_for(url)
    assert (rules.name if rules else None) == expected


def test_a_hostname_is_matched_whole():
    """`endswith` on a bare suffix would make `evilmyworkdayjobs.com` a
    Workday tenant; the match is the host or a dotted suffix of it."""
    assert table.rules_for("https://evilmyworkdayjobs.com/x") is None
    assert table.rules_for("https://x.myworkdayjobs.com/y") is not None


def test_continue_and_submit_are_never_the_same_expression():
    """The two controls carry the same `data-automation-id` on Workday and
    differ only by their text. An edit that lost the difference would make
    the walk's Continue the form's Submit, so it is checked rather than
    trusted."""
    for name, rules in table.load().items():
        advance, submit = rules.page("advance"), rules.page("submit")
        if advance and submit:
            assert advance != submit, name
            assert "Submit" in submit, name
            assert "not(contains(., 'Submit')" in advance, name


def test_a_step_cannot_name_a_method_that_does_not_exist():
    rules = table._rules("acme", {
        "hosts": ["acme.test"],
        "fields": {"resume": {"paths": [".//div"],
                              "steps": [{"method": "press", "path": "%FIELD%//button"}]}},
    })
    assert any("'press' is not a method" in why for why in table.invalid(rules))


def test_a_field_path_cannot_be_written_against_itself():
    rules = table._rules("acme", {"hosts": ["acme.test"],
                                  "fields": {"x": {"paths": [".//div[%FIELD%]"]}}})
    assert any("cannot use %FIELD%" in why for why in table.invalid(rules))


def test_an_upload_must_say_which_document_it_puts_on():
    rules = table._rules("acme", {
        "hosts": ["acme.test"],
        "fields": {"resume": {"paths": [".//div"],
                              "steps": [{"method": "upload", "path": "%FIELD%//input"}]}},
    })
    assert any("names no document" in why for why in table.invalid(rules))


def test_a_step_cannot_wait_on_a_param_the_system_does_not_have():
    rules = table._rules("acme", {
        "hosts": ["acme.test"], "params": {"settle": 0.25},
        "fields": {"resume": {"paths": [".//div"],
                              "steps": [{"method": "expect_gone", "path": "%FIELD%//div",
                                         "wait": "forever"}]}},
    })
    assert any("not a param" in why for why in table.invalid(rules))


@pytest.mark.parametrize("expr,why", [
    ("", "empty"),
    ("div[@id='x']", "does not start"),
    (".//div[@id='x'", "unbalanced"),
    (".//div[@id='x]", "unbalanced quotes"),
])
def test_the_cheap_mistakes_in_a_hand_written_expression_are_caught(expr, why):
    rules = table._rules("acme", {"hosts": ["acme.test"], "pages": {"review": expr}})
    assert any(why in problem for problem in table.invalid(rules))


def test_a_page_key_the_runner_does_not_know_is_refused():
    rules = table._rules("acme", {"hosts": ["acme.test"], "pages": {"finish": ".//div"}})
    assert any("is not one of" in why for why in table.invalid(rules))


def test_the_timings_are_the_systems_own_and_not_constants():
    """`browser/workday.py` carries one hand-picked `ADVANCE_TIMEOUT` for
    every tenant. A system's own numbers live with its selectors."""
    workday = table.load()["workday"]
    assert workday.param("advance") > 0
    assert workday.param("verify") > 0
    assert workday.param("nothing_named_this", 7.0) == 7.0


def test_a_field_with_no_steps_is_a_text_box():
    workday = table.load()["workday"]
    assert workday.field("first_name").is_text
    assert not workday.field("resume").is_text
    assert [s.method for s in workday.field("resume").steps] == ["click", "upload", "expect_name"]


def test_the_resume_clears_the_slot_before_it_uploads():
    """Workday keeps the input *and* the attachment, so an upload with no
    clear in front of it puts a second resume on the slot - and the clear
    has to be allowed to fail, because an empty slot has no delete button
    (2026-09-30, 2026-10-09)."""
    steps = table.load()["workday"].field("resume").steps
    assert steps[0].method == "click" and steps[0].optional
    assert steps[0].repeat > 1, "a dropzone keeps a row per file"
    assert steps[1].method == "upload"
    assert steps[2].method == "expect_name", "the filename is read back, never assumed"
