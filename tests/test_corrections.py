"""The correction loop: the form as the agent left it against the form as
the human submitted it; every change becomes a correction on file with
what the form held before, visa questions never do, the next fill puts
the corrected value over autofill by label, and nothing here needs a
browser."""
import json

import pytest

from browser.forms import profile as form_profile
from server import corrections


def test_diff_keeps_what_the_human_filled_in_or_changed():
    baseline = {"First Name": "Jane", "Phone": "555", "Notice period": "", "Why us?": ""}
    final = {"First Name": "Jane", "Phone": "777", "Notice period": "2 weeks", "Why us?": "",
             "Will you require sponsorship?": "No"}
    assert corrections.diff(baseline, final) == {"Phone": "777", "Notice period": "2 weeks"}


def test_clearing_a_field_teaches_nothing():
    assert corrections.diff({"Phone": "555"}, {"Phone": ""}) == {}


def _app(tmp_path):
    app = tmp_path / "acme_swe"
    app.mkdir()
    (app / corrections.FILL_REPORT).write_text(json.dumps({
        "report": {"ats": "greenhouse"},
        "after_agent": {"Phone": "555", "Pronouns": "", "Location": "Delhi, India"},
        "after_agent_meta": {"Location": {"id": "job_application_location", "kind": "combobox"}}}))
    (app / corrections.FORM_STATE).write_text(json.dumps({
        "fields": {"Phone": "555", "Pronouns": "she/her", "Location": "Los Angeles, California, United States",
                   "Do you need a visa?": "No"},
        "meta": {"Location": {"id": "job_application_location", "kind": "combobox"}}}))
    return app


def test_changes_are_offered_never_decided(tmp_path):
    app = _app(tmp_path)
    rows = corrections.changes(app, store={})
    assert [(r["label"], r["was"], r["now"], r["remembered"]) for r in rows] == [
        ("Pronouns", "", "she/her", False),
        ("Location", "Delhi, India", "Los Angeles, California, United States", False),
    ]
    # A row already on file with that value shows as remembered.
    on_file = {"location *": {"value": "Los Angeles, California, United States"}}
    assert [r["remembered"] for r in corrections.changes(app, store=on_file)] == [False, True]
    assert corrections.changes(tmp_path) == []


def test_remember_writes_only_what_was_picked(tmp_path):
    app = _app(tmp_path)
    form = tmp_path / "form.json"
    result = corrections.remember(app, ["Location"], form)
    assert result["remembered"] == 1 and result["changes"] == {"Location": "Los Angeles, California, United States"}
    saved = json.loads(form.read_text())["corrections"]
    assert list(saved) == ["Location"], "Pronouns was not picked"
    location = saved["Location"]
    assert location["was"] == "Delhi, India" and location["system"] == "greenhouse" and location["job"] == "acme_swe"
    assert location["field"]["id"] == "job_application_location" and location["when"]
    assert "Los Angeles" in (app / corrections.NOTES).read_text()
    assert form_profile.load_corrections(form).answer("location *") == "Los Angeles, California, United States"
    # Nothing picked, or a label that did not change: nothing written.
    assert corrections.remember(app, [], form)["remembered"] == 0
    assert corrections.remember(app, ["Phone"], form)["remembered"] == 0
    assert corrections.remember(app, ["Do you need a visa?"], form)["remembered"] == 0


def test_nothing_is_learned_when_a_job_is_marked_submitted():
    import inspect
    import server.review as review
    for name in ("mark_submitted", "mark_seen"):
        source = inspect.getsource(getattr(review, name))
        assert "corrections.learn" not in source and "add_corrections" not in source and "remember(" not in source, name
        assert "corrections.changes" in source, "the pick list is still offered"
    assert not hasattr(corrections, "learn")


def test_add_corrections_merges_dedupes_by_normalised_label_and_migrates_answers(tmp_path):
    form = tmp_path / "form.json"
    form.write_text(json.dumps({"first_name": "J", "answers": {"Notice period?": "1 week", "_comment": "x",
                                                                "Are you willing to relocate?": "Yes"}}))
    assert form_profile.add_corrections({"Notice period": "2 weeks", "": "no", "Blank": ""}, form,
                                        system="lever", before={"Notice period": "1 week"}) == 1
    data = json.loads(form.read_text())
    assert data["first_name"] == "J"
    assert "answers" not in data
    assert set(data["corrections"]) == {"Notice period", "Are you willing to relocate?"}
    assert data["corrections"]["Notice period"]["value"] == "2 weeks"
    assert data["corrections"]["Notice period"]["was"] == "1 week"
    assert data["corrections"]["Notice period"]["system"] == "lever"
    assert data["corrections"]["Are you willing to relocate?"] == {"value": "Yes", "was": ""}
    assert form_profile.load(form).answer("notice period ?") == "2 weeks"
    assert form_profile.load(form).correction("NOTICE PERIOD")["was"] == "1 week"


def test_a_form_with_only_corrections_still_loads_the_store(tmp_path):
    form = tmp_path / "form.json"
    form.write_text(json.dumps({"corrections": {"Location": {"value": "LA", "was": "Delhi"}}}))
    assert form_profile.load(form) is None            # no contact details: the code filler steps aside
    store = form_profile.load_corrections(form)
    assert store.answer("Location") == "LA" and store.correction("location")["was"] == "Delhi"
    assert not form_profile.load_corrections(tmp_path / "missing.json").corrections
