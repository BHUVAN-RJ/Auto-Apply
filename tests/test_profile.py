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
    (stories / "payments" / "tailor.md").write_text(
        "Built the payments service.\nLink: https://example.com/pay\nStack: Go, Postgres\n")
    (stories / "search").mkdir()
    (stories / "search" / "tailor.md").write_text(
        "Rebuilt search on Elasticsearch.\nStack: Elasticsearch, Java\n")
    (stories / "index.md").write_text("- [payments] Payments service. Go, Postgres\n"
                                      "- [search] Search. Elasticsearch\n")
    # The resume the picker measures "already on the resume" against.
    monkeypatch.setattr(profile, "BASE_RESUME", tmp_path / "resume.tex")
    (tmp_path / "resume.tex").write_text(
        r"\begin{document}\item{Rebuilt search on Elasticsearch}\end{document}")


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


def test_pick_keeps_only_known_slugs_and_leads_with_the_swap_candidates(monkeypatch):
    """Made-up slugs are dropped; the stories the resume does not carry come
    first, because that is the order the tailor reads them in and a swap is
    the edit it is most likely to skip."""
    monkeypatch.setattr(profile.llm, "complete",
                        lambda *a, **k: "- [search]\nmade-up\npayments\n")
    assert profile.pick("posting text") == ["payments", "search"]


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
    assert settings.load() == {"use_profile": True, "auto_fill": True, "auto_learn": False, "learn_prompts": True, "onboarded": False, "scout_checks_per_day": 4}
    settings.save(use_profile=False)
    assert settings.use_profile() is False
    assert settings.auto_fill() is True
    settings.save(auto_fill=False)
    assert settings.auto_fill() is False and settings.use_profile() is False
    assert settings.auto_learn() is False
    settings.save(auto_learn=True)
    assert settings.auto_learn() is True and settings.auto_fill() is False


def stub_pick(monkeypatch, reply):
    monkeypatch.setattr(profile.llm, "complete", lambda *a, **k: reply)


def test_a_story_the_resume_already_carries_is_recognised(tmp_path):
    resume = (tmp_path / "resume.tex").read_text()
    assert profile.on_resume("search", resume) is True
    assert profile.on_resume("payments", resume) is False
    # A link on the page is the other route, for a slug whose words are not.
    linked = resume + r"\href{https://example.com/pay}{Pay}"
    assert profile.on_resume("payments", linked,
                             profile.story_text("payments")) is True


def test_the_picker_cannot_spend_every_slot_on_what_is_already_on_the_resume(monkeypatch):
    """The swap pool is reserved. A model that ranks the on-resume story first
    still hands the tailor a candidate, because a run with no candidate is a
    run that cannot swap a project."""
    monkeypatch.setattr(profile, "DEPTH_SLOTS", 1)
    monkeypatch.setattr(profile, "SWAP_SLOTS", 2)
    stub_pick(monkeypatch, "search\npayments\n")
    assert profile.pick("Go and Postgres") == ["payments", "search"]


def test_a_pool_left_short_is_filled_from_the_stories_the_picker_passed_over(monkeypatch):
    """Nothing stays on the shelf while the posting names its stack: the
    swapper going out of stock is what left 18 of 36 jobs with no swap."""
    stub_pick(monkeypatch, "search\n")
    assert profile.pick("We run Go and Postgres") == ["payments", "search"]
    # Nothing shared with the posting, nothing forced in.
    stub_pick(monkeypatch, "search\n")
    assert profile.pick("We are a Salesforce shop") == ["search"]


def test_the_stories_say_whether_the_resume_already_carries_them():
    text = profile.context(slugs=["payments", "search"])
    assert "### payments (not on the resume" in text
    assert "### search (already on the resume" in text


def test_the_index_the_picker_reads_is_marked(tmp_path):
    marked = profile.marked_index(profile.index(), (tmp_path / "resume.tex").read_text())
    assert "[search] Search. Elasticsearch (already on the resume)" in marked
    assert "[payments] Payments service. Go, Postgres\n" in marked + "\n"
