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
    stories = tmp_path / "stories"
    (stories / "payments").mkdir(parents=True)
    (stories / "payments" / "main.md").write_text("Long transcript-shaped document.")
    (stories / "payments" / "tailor.md").write_text("Built the payments service.")
    (stories / "search").mkdir()
    (stories / "search" / "tailor.md").write_text("Rebuilt search on Elasticsearch.")
    (stories / "index.md").write_text("- [payments] Payments service. Go, Postgres\n"
                                      "- [search] Search. Elasticsearch\n")


def test_switch_on_adds_facts_and_index_but_no_story_without_a_pick():
    text = profile.context()
    assert "Backend engineer." in text
    assert "- Years: 2" in text and "Phone" not in text
    assert "[payments]" in text and "[search]" in text
    assert "payments service" not in text and "transcript-shaped" not in text


def test_picked_stories_are_the_tailor_docs_only():
    text = profile.context(slugs=["payments"])
    assert "### payments" in text and "payments service" in text
    assert "### search" not in text
    assert "transcript-shaped" not in text


def test_pick_keeps_only_known_slugs_in_the_models_order(monkeypatch):
    monkeypatch.setattr(profile.llm, "complete",
                        lambda *a, **k: "- [search]\nmade-up\npayments\n")
    assert profile.pick("posting text") == ["search", "payments"]


def test_pick_is_empty_with_no_index_or_switch_off(tmp_path, monkeypatch):
    monkeypatch.setattr(profile.llm, "complete",
                        lambda *a, **k: pytest.fail("model called with nothing to pick from"))
    settings.save(use_profile=False)
    assert profile.pick("posting") == []
    settings.save(use_profile=True)
    (tmp_path / "stories" / "index.md").unlink()
    assert profile.pick("posting") == []


def test_read_used(tmp_path):
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    assert profile.read_used(app_dir) == []
    (app_dir / "stories_used.txt").write_text("payments\n\nsearch\n")
    assert profile.read_used(app_dir) == ["payments", "search"]


def test_switch_off_is_the_profile_alone():
    settings.save(use_profile=False)
    assert profile.context() == "Backend engineer."


def test_switch_on_with_nothing_written_is_the_profile_alone(tmp_path):
    (tmp_path / "applicant.md").unlink()
    (tmp_path / "stories" / "index.md").unlink()
    assert profile.context() == "Backend engineer."


def test_story_dirs_skip_interviewer_state(tmp_path):
    (tmp_path / "stories" / "_interview").mkdir()
    assert [p.name for p in profile.story_dirs()] == ["payments", "search"]


def test_settings_default_and_roundtrip():
    assert settings.load() == {"use_profile": True, "auto_fill": True, "auto_learn": False, "scout_checks_per_day": 4}
    settings.save(use_profile=False)
    assert settings.use_profile() is False
    assert settings.auto_fill() is True
    settings.save(auto_fill=False)
    assert settings.auto_fill() is False and settings.use_profile() is False
    assert settings.auto_learn() is False
    settings.save(auto_learn=True)
    assert settings.auto_learn() is True and settings.auto_fill() is False
