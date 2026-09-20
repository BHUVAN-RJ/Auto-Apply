"""The preliminary interview endpoint: questions with stable keys, a save
that keeps the correction store and pins the source to Other, and the
authorisation block collected but never handed to the filler."""
import json

import pytest
from fastapi.testclient import TestClient

from browser.forms import profile as form_profile
from server import form
from server.app import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(form_profile, "FORM", tmp_path / "form.json")
    return TestClient(app)


def test_questions_cover_the_filler_keys_and_the_authorisation_block():
    keys = set(form.KEYS)
    assert {"first_name", "last_name", "email", "phone", "linkedin", "city", "state", "country",
            "school", "degree", "how_heard", "gender"} <= keys
    protected = {q["key"] for q in form.QUESTIONS if q.get("protected")}
    assert {"us_status", "needs_sponsorship", "work_authorized", "citizenship"} <= protected
    # The filler's profile never carries the authorisation answers.
    assert not protected & set(form_profile.KEYS)


def test_save_writes_the_file_keeps_corrections_and_pins_the_source(client, tmp_path):
    first = client.get("/profile/form").json()
    assert first["exists"] is False and first["total"] == len(form.KEYS)
    saved = client.post("/profile/form", json={"values": {"first_name": "Jane", "how_heard": "Jobright", "us_status": "F-1 OPT",
                                                           "salary": "  "},
                                               "corrections": {"Notice period": "2 weeks", "_comment": "x", "Blank": "",
                                                               "Location": {"value": "LA", "was": "Delhi", "system": "ashby"}}}).json()
    assert saved["values"]["how_heard"] == "Other" and saved["values"]["first_name"] == "Jane"
    assert "salary" not in saved["values"]
    assert saved["corrections"] == {"Notice period": {"value": "2 weeks", "was": ""},
                                    "Location": {"value": "LA", "was": "Delhi", "system": "ashby"}}
    on_disk = json.loads((tmp_path / "form.json").read_text())
    assert on_disk["us_status"] == "F-1 OPT"
    # A second save without corrections leaves the store alone.
    again = client.post("/profile/form", json={"values": {"first_name": "Jan"}}).json()
    assert set(again["corrections"]) == {"Notice period", "Location"} and again["values"]["first_name"] == "Jan"
    # A delete on the page is a save with the row gone.
    fewer = client.post("/profile/form", json={"values": {"first_name": "Jan"},
                                               "corrections": {"Notice period": {"value": "2 weeks"}}}).json()
    assert set(fewer["corrections"]) == {"Notice period"}
    # And the filler reads the same file: the authorisation answer is not a value it can fill.
    profile = form_profile.load(tmp_path / "form.json")
    assert profile.get("first_name") == "Jan" and profile.get("us_status") == ""
    assert profile.answer("notice period") == "2 weeks"


def test_a_legacy_answers_key_is_read_as_corrections_and_rewritten(client, tmp_path):
    (tmp_path / "form.json").write_text(json.dumps({"first_name": "Jane", "answers": {"Notice period": "2 weeks", "_comment": "x"}}))
    got = client.get("/profile/form").json()
    assert got["corrections"] == {"Notice period": {"value": "2 weeks", "was": ""}}
    client.post("/profile/form", json={"values": {"first_name": "Jane"}})
    on_disk = json.loads((tmp_path / "form.json").read_text())
    assert "answers" not in on_disk and on_disk["corrections"]["Notice period"]["value"] == "2 weeks"
