"""The profile switch: what the models are told about the applicant."""

import pytest

from server import settings
from tailor import profile


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(profile, "PROFILE", tmp_path / "profile.md")
    monkeypatch.setattr(profile, "APPLICANT", tmp_path / "applicant.md")
    monkeypatch.setattr(profile, "STORIES", tmp_path / "stories")
    (tmp_path / "profile.md").write_text("Backend engineer.")
    (tmp_path / "applicant.md").write_text("## Facts\n\n- Years: 2\n\n## Form details\n\n- Phone: 1\n")
    (tmp_path / "stories").mkdir()
    (tmp_path / "stories" / "payments.md").write_text("Built the payments service.")


def test_switch_on_adds_facts_and_stories():
    text = profile.context()
    assert "Backend engineer." in text
    assert "- Years: 2" in text and "Phone" not in text
    assert "### payments" in text and "payments service" in text


def test_switch_off_is_the_profile_alone():
    settings.save(use_profile=False)
    assert profile.context() == "Backend engineer."


def test_switch_on_with_nothing_written_is_the_profile_alone(tmp_path):
    (tmp_path / "applicant.md").unlink()
    (tmp_path / "stories" / "payments.md").unlink()
    assert profile.context() == "Backend engineer."


def test_stories_are_capped(tmp_path):
    (tmp_path / "stories" / "a.md").write_text("x" * 100)
    (tmp_path / "stories" / "b.md").write_text("y" * 100)
    text = profile.stories(cap=150)
    assert "### a" in text and "### b" not in text


def test_settings_default_and_roundtrip():
    assert settings.load() == {"use_profile": True}
    settings.save(use_profile=False)
    assert settings.use_profile() is False
