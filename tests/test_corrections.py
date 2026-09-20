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


def test_learn_writes_corrections_with_what_the_form_held_and_a_note(tmp_path):
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
    form = tmp_path / "form.json"
    result = corrections.learn(app, form)
    assert result["learned"] == 2
    assert result["changes"] == {"Pronouns": "she/her", "Location": "Los Angeles, California, United States"}
    saved = json.loads(form.read_text())["corrections"]
    location = saved["Location"]
    assert location["value"] == "Los Angeles, California, United States"
    assert location["was"] == "Delhi, India"
    assert location["system"] == "greenhouse" and location["job"] == "acme_swe"
    assert location["field"]["id"] == "job_application_location" and location["when"]
    assert saved["Pronouns"]["was"] == "" and saved["Pronouns"]["value"] == "she/her"
    assert "she/her" in (app / corrections.NOTES).read_text()
    # The next fill reads it back as the value to put on that label.
    assert form_profile.load_corrections(form).answer("location *") == "Los Angeles, California, United States"


def test_learn_without_a_snapshot_is_a_note_not_an_error(tmp_path):
    assert corrections.learn(tmp_path)["learned"] == 0
    (tmp_path / corrections.FORM_STATE).write_text(json.dumps({"fields": {"A": "b"}}))
    assert "baseline" in corrections.learn(tmp_path)["reason"]


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


def test_the_submitted_endpoints_learn_but_never_depend_on_it(monkeypatch):
    import inspect
    import server.review as review
    for name in ("mark_submitted", "mark_seen"):
        source = inspect.getsource(getattr(review, name))
        assert "corrections.learn" in source
        assert source.index("Status.SUBMITTED") < source.index("corrections.learn"), name
    assert "mark_seen(" in inspect.getsource(review.confirmation_seen)
