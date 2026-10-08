"""The Settings tab's API: keys go in, values never come back out."""

from __future__ import annotations

import smtplib

import httpx
import pytest
from fastapi.testclient import TestClient

import paths
from server import keys
from server.app import app


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A data directory of its own, so no test can reach the real `.env`."""
    env = tmp_path / ".env"
    env.write_text("AUTOPILOT_SPLIT=0\n")
    monkeypatch.setattr(paths, "ENV_FILE", env)
    monkeypatch.setattr(paths, "HOME", tmp_path)
    for secret in keys.CATALOG:
        monkeypatch.delenv(secret.name, raising=False)
    return env


@pytest.fixture
def client(home):
    return TestClient(app)


def test_the_catalog_lists_every_key_and_nothing_else():
    """A key is a credential the person holds and must be able to replace.
    A switch is a decision about behaviour and belongs in `.env`, so one
    appearing here would be this tab growing past what it promises."""
    assert [s.name for s in keys.CATALOG] == [
        "OPENROUTER_API_KEY",
        "AUTOPILOT_APOLLO_API_KEY",
        "AUTOPILOT_SMTP_USER",
        "AUTOPILOT_SMTP_PASS",
        "AUTOPILOT_MAIL_TO",
        "AUTOPILOT_GITHUB_TOKEN",
        "AUTOPILOT_FISH_API_KEY",
    ]
    assert [s for s in keys.CATALOG if s.required] == [keys.BY_NAME["OPENROUTER_API_KEY"]]
    # Every checkable key names a checker that exists.
    for secret in keys.CATALOG:
        if secret.check:
            assert secret.check in keys.CHECKS, secret.name


def test_a_saved_key_is_never_handed_back(client, home, monkeypatch):
    key = "sk-or-" + "v1abcdefghijklmnopqrstuvwxyz0123456789"
    monkeypatch.setattr(keys, "CHECKS", dict(keys.CHECKS))
    answer = client.post("/keys/OPENROUTER_API_KEY", json={"value": key})
    assert answer.status_code == 200, answer.text
    row = next(r for r in answer.json()["keys"] if r["name"] == "OPENROUTER_API_KEY")
    assert row["set"] is True
    assert row["tail"] == key[-4:]
    # The whole body, not just the row: nothing anywhere carries the value.
    assert key not in answer.text
    assert client.get("/keys").text.count(key) == 0


def test_the_key_reaches_the_env_file_and_the_process(client, home):
    key = "sk-or-" + "v1" + "z" * 40
    client.post("/keys/OPENROUTER_API_KEY", json={"value": key})
    assert f"OPENROUTER_API_KEY={key}" in home.read_text()
    # Written there and into this process, so scripts started from now on
    # see it without a restart.
    import os
    assert os.environ["OPENROUTER_API_KEY"] == key
    # Every other line of the file is kept.
    assert "AUTOPILOT_SPLIT=0" in home.read_text()
    assert oct(home.stat().st_mode)[-3:] == "600"


def test_a_typing_mistake_is_refused_as_one(client, home):
    bad = [
        ("OPENROUTER_API_KEY", "hf_not_an_openrouter_key_at_all_really"),
        ("OPENROUTER_API_KEY", "sk-or-short"),
        ("OPENROUTER_API_KEY", "sk-or-v1" + "a" * 30 + "\nsecond line"),
        ("AUTOPILOT_SMTP_USER", "not-an-address"),
        ("AUTOPILOT_SMTP_PASS", "tooshort"),
        ("AUTOPILOT_APOLLO_API_KEY", "short"),
    ]
    for name, value in bad:
        answer = client.post(f"/keys/{name}", json={"value": value})
        assert answer.status_code == 400, (name, value, answer.text)
    assert "OPENROUTER_API_KEY" not in home.read_text()


def test_an_app_password_keeps_its_spaces_out(client, home):
    """Gmail shows an App Password in groups of four; it does not want them
    back, and a person pastes what they are shown."""
    client.post("/keys/AUTOPILOT_SMTP_PASS", json={"value": "abcd efgh ijkl mnop"})
    assert "AUTOPILOT_SMTP_PASS=abcdefghijklmnop" in home.read_text()


def test_an_address_is_shown_whole_and_a_key_is_not(client, home):
    client.post("/keys/AUTOPILOT_SMTP_USER", json={"value": "someone@example.com"})
    client.post("/keys/AUTOPILOT_APOLLO_API_KEY", json={"value": "a" * 24})
    rows = {r["name"]: r for r in client.get("/keys").json()["keys"]}
    assert rows["AUTOPILOT_SMTP_USER"]["tail"] == "someone@example.com"
    assert rows["AUTOPILOT_SMTP_USER"]["password"] is False
    assert rows["AUTOPILOT_APOLLO_API_KEY"]["tail"] == "aaaa"


def test_the_required_key_can_be_replaced_but_not_removed(client, home):
    key = "sk-or-v1" + "b" * 40
    client.post("/keys/OPENROUTER_API_KEY", json={"value": key})
    assert client.delete("/keys/OPENROUTER_API_KEY").status_code == 409
    assert f"OPENROUTER_API_KEY={key}" in home.read_text()
    # Replacing it is the way to change it.
    other = "sk-or-v1" + "c" * 40
    client.post("/keys/OPENROUTER_API_KEY", json={"value": other})
    assert f"OPENROUTER_API_KEY={other}" in home.read_text()
    assert key not in home.read_text()


def test_forgetting_a_key_takes_it_out_of_both_places(client, home):
    import os
    client.post("/keys/AUTOPILOT_GITHUB_TOKEN", json={"value": "ghp_" + "d" * 30})
    answer = client.delete("/keys/AUTOPILOT_GITHUB_TOKEN")
    assert answer.status_code == 200
    assert "AUTOPILOT_GITHUB_TOKEN" not in os.environ
    assert "AUTOPILOT_GITHUB_TOKEN=\n" in home.read_text()
    row = next(r for r in answer.json()["keys"] if r["name"] == "AUTOPILOT_GITHUB_TOKEN")
    assert row["set"] is False


def test_a_key_the_catalog_does_not_name_is_a_404(client, home):
    assert client.get("/keys").status_code == 200
    assert client.post("/keys/AUTOPILOT_SPLIT", json={"value": "0"}).status_code == 404
    assert client.delete("/keys/OPENROUTER_PREMIUM_MODEL").status_code == 404
    assert client.post("/keys/nonsense/check").status_code == 404


def test_missing_says_only_what_the_app_cannot_run_without(client, home):
    assert client.get("/keys").json()["missing"] == ["OpenRouter key"]
    client.post("/keys/OPENROUTER_API_KEY", json={"value": "sk-or-v1" + "e" * 40})
    assert client.get("/keys").json()["missing"] == []


def test_a_check_is_asked_for_and_never_a_consequence_of_saving(client, home, monkeypatch):
    """Saving costs nothing; checking costs a request to somebody else's
    service, so the two are separate calls."""
    asked = []
    monkeypatch.setattr(keys, "CHECKS",
                        {**keys.CHECKS, "openrouter": lambda values: asked.append(values) or "fine"})
    client.post("/keys/OPENROUTER_API_KEY", json={"value": "sk-or-v1" + "f" * 40})
    assert asked == []
    answer = client.post("/keys/OPENROUTER_API_KEY/check")
    assert answer.status_code == 200 and answer.json()["said"] == "fine"
    assert len(asked) == 1


def test_an_unset_key_is_not_checked(client, home):
    assert client.post("/keys/AUTOPILOT_APOLLO_API_KEY/check").status_code == 409
    # And one with nothing to check against says so rather than pretending.
    client.post("/keys/AUTOPILOT_MAIL_TO", json={"value": "someone@example.com"})
    assert client.post("/keys/AUTOPILOT_MAIL_TO/check").status_code == 409


def test_a_check_covers_the_whole_account(client, home, monkeypatch):
    """The Gmail address and its App Password are one login, so checking
    either one logs in with both."""
    seen = {}
    monkeypatch.setattr(keys, "CHECKS", {**keys.CHECKS, "mail": lambda values: seen.update(values) or "ok"})
    client.post("/keys/AUTOPILOT_SMTP_USER", json={"value": "someone@example.com"})
    client.post("/keys/AUTOPILOT_SMTP_PASS", json={"value": "abcdefghijkl"})
    assert client.post("/keys/AUTOPILOT_SMTP_PASS/check").status_code == 200
    assert seen["AUTOPILOT_SMTP_USER"] == "someone@example.com"
    assert seen["AUTOPILOT_SMTP_PASS"] == "abcdefghijkl"


def test_a_refused_key_is_the_person_s_mistake_and_a_dead_service_is_not(home, monkeypatch):
    """401 is a bad key, 400; anything else is the service's problem, 502.
    Reading them the same way would send somebody to re-copy a key that
    was fine."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1" + "g" * 40)

    def answer(status, body=b"{}"):
        return httpx.Response(status, content=body, headers={"content-type": "application/json"})

    monkeypatch.setattr(keys.httpx, "get", lambda *a, **k: answer(401))
    with pytest.raises(Exception) as refused:
        keys.CHECKS["openrouter"]({"OPENROUTER_API_KEY": "x"})
    assert refused.value.status_code == 400

    monkeypatch.setattr(keys.httpx, "get", lambda *a, **k: answer(503))
    with pytest.raises(Exception) as broken:
        keys.CHECKS["openrouter"]({"OPENROUTER_API_KEY": "x"})
    assert broken.value.status_code == 502


def test_the_mail_check_logs_in_and_sends_nothing(home, monkeypatch):
    sent = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=0):
            sent.append(("connect", host, port))

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def ehlo(self):
            pass

        def starttls(self):
            pass

        def login(self, user, password):
            sent.append(("login", user, password))

        def send_message(self, msg):  # pragma: no cover - the point is it is not called
            sent.append(("send", msg))

    monkeypatch.setattr(keys.smtplib, "SMTP", FakeSMTP)
    said = keys.CHECKS["mail"]({"AUTOPILOT_SMTP_USER": "a@b.com", "AUTOPILOT_SMTP_PASS": "pw"})
    assert "Nothing was sent" in said
    assert [row[0] for row in sent] == ["connect", "login"]


def test_a_refused_login_says_app_password(home, monkeypatch):
    class Refusing:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def ehlo(self):
            pass

        def starttls(self):
            pass

        def login(self, *a):
            raise smtplib.SMTPAuthenticationError(535, b"nope")

    monkeypatch.setattr(keys.smtplib, "SMTP", Refusing)
    with pytest.raises(Exception) as refused:
        keys.CHECKS["mail"]({"AUTOPILOT_SMTP_USER": "a@b.com", "AUTOPILOT_SMTP_PASS": "pw"})
    assert refused.value.status_code == 400
    assert "App Password" in refused.value.detail


def test_the_page_offers_the_tab_and_asks_for_the_keys():
    """The tab, its switcher branch, and the module: a tab button with no
    branch behind it is a click that does nothing."""
    page = (paths.ROOT / "review" / "index.html").read_text()
    assert 'id="tabSettings"' in page
    assert '$("tabSettings").onclick = () => switchView("settings")' in page
    assert 'else if (name === "settings") Settings.open();' in page
    assert "const Settings = (() => {" in page
    # It is a page of fields, not a set of things to pick between.
    assert 'name === "screening" || name === "settings"' in page
    # And the value of a key is never put into the markup of a saved row.
    module = page.split("const Settings = (() => {")[1].split("\nconst Screening")[0]
    assert "typed[name] = \"\";" in module
