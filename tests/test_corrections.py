"""The correction loop: the form as the agent left it against the form as
the human submitted it; every change becomes an answer on file, visa
questions never do, and nothing here needs a browser."""
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


def test_learn_writes_answers_and_a_note(tmp_path):
    app = tmp_path / "app"
    app.mkdir()
    (app / corrections.FILL_REPORT).write_text(json.dumps({"after_agent": {"Phone": "555", "Pronouns": ""}}))
    (app / corrections.FORM_STATE).write_text(json.dumps({"fields": {"Phone": "555", "Pronouns": "she/her",
                                                                       "Do you need a visa?": "No"}}))
    form = tmp_path / "form.json"
    result = corrections.learn(app, form)
    assert result["learned"] == 1 and result["changes"] == {"Pronouns": "she/her"}
    assert json.loads(form.read_text())["answers"] == {"Pronouns": "she/her"}
    assert "she/her" in (app / corrections.NOTES).read_text()


def test_learn_without_a_snapshot_is_a_note_not_an_error(tmp_path):
    assert corrections.learn(tmp_path)["learned"] == 0
    (tmp_path / corrections.FORM_STATE).write_text(json.dumps({"fields": {"A": "b"}}))
    assert "baseline" in corrections.learn(tmp_path)["reason"]


def test_add_answers_merges_and_dedupes_by_normalised_label(tmp_path):
    form = tmp_path / "form.json"
    form.write_text(json.dumps({"first_name": "J", "answers": {"Notice period?": "1 week", "_comment": "x"}}))
    assert form_profile.add_answers({"Notice period": "2 weeks", "": "no", "Blank": ""}, form) == 1
    data = json.loads(form.read_text())
    assert data["first_name"] == "J"
    assert data["answers"] == {"_comment": "x", "Notice period": "2 weeks"}
    assert form_profile.load(form).answer("notice period ?") == "2 weeks"


def test_the_submitted_endpoints_learn_but_never_depend_on_it(monkeypatch):
    import inspect
    import server.review as review
    for name in ("mark_submitted", "confirmation_seen"):
        source = inspect.getsource(getattr(review, name))
        assert "corrections.learn" in source
        assert source.index("Status.SUBMITTED") < source.index("corrections.learn"), name
