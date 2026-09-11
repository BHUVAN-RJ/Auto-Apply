"""The fill loop's wiring: the guard actually intercepts, and node reading works."""

import asyncio
import types
from pathlib import Path

import pytest

from browser import fill, guard


class FakeNode:
    def __init__(self, text=None, attributes=None, ax_name=None):
        self._text = text
        self.attributes = attributes or {}
        self.node_value = None
        self.ax_node = types.SimpleNamespace(name=ax_name) if ax_name else None

    def get_all_children_text(self):
        if self._text is None:
            raise RuntimeError("no text on this node")
        return self._text


class FakeSession:
    def __init__(self, node):
        self.node = node

    async def get_element_by_index(self, index):
        return self.node


def test_describe_reads_text_and_attributes():
    node = FakeNode("Submit application", {"id": "btn", "aria-label": "Send it"})
    described = fill.describe(node)
    assert described["text"] == "Submit application"
    assert described["id"] == "btn"
    assert described["aria_label"] == "Send it"


def test_describe_falls_back_to_the_accessibility_name():
    node = FakeNode(text=None, attributes={}, ax_name="Submit application")
    assert fill.describe(node)["text"] == "Submit application"


def test_describe_survives_a_node_it_does_not_understand():
    assert fill.describe(None) == {}
    assert fill.describe(object())["text"] is None


def test_forbidden_actions_are_absent_from_the_agent(monkeypatch):
    tools = fill._build_tools()
    actions = tools.registry.registry.actions
    for name in guard.FORBIDDEN_ACTIONS:
        assert name not in actions, f"{name} must not be available to the agent"
    assert "click" in actions and "upload_file" in actions


def call_real_guarded_click(node):
    """Invoke the genuinely wrapped click action, the way browser-use does.

    Exercises the real function off the registry rather than a reconstruction
    of it, so the test fails if the wiring in _build_tools ever breaks.
    """
    tools = fill._build_tools()
    guarded = tools.registry.registry.actions["click"].function
    return asyncio.run(
        guarded(params=types.SimpleNamespace(index=1), browser_session=FakeSession(node))
    )


def test_guarded_click_blocks_a_submit_button():
    with pytest.raises(guard.SubmitBlocked):
        call_real_guarded_click(FakeNode("Submit application"))


def test_guarded_click_blocks_an_icon_button_with_a_submit_aria_label():
    node = FakeNode(text="", attributes={"aria-label": "Submit application"})
    with pytest.raises(guard.SubmitBlocked):
        call_real_guarded_click(node)


def test_guarded_click_blocks_a_button_identified_only_by_its_id():
    node = FakeNode(text="", attributes={"id": "submit_application"})
    with pytest.raises(guard.SubmitBlocked):
        call_real_guarded_click(node)


def test_guarded_click_passes_an_ordinary_button_through_to_browser_use():
    """The guard must not block navigation; the call reaches the real click,
    which then fails on the fake session — any error except SubmitBlocked."""
    try:
        call_real_guarded_click(FakeNode("Save and continue"))
    except guard.SubmitBlocked:
        pytest.fail("an ordinary button must not be blocked")
    except Exception:
        pass  # the real click rejecting a fake session is the expected outcome


def test_the_real_click_action_is_wrapped_not_replaced():
    """The guard must sit in front of browser-use's own click, not replace it."""
    import inspect

    source = inspect.getsource(fill._build_tools)
    assert "inner = original.function" in source
    assert "await inner(" in source


def test_fill_requires_an_api_key(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    with pytest.raises(fill.FillError, match="OPENROUTER_API_KEY"):
        asyncio.run(fill.fill_async("https://example.com", tmp_path / "r.pdf",
                                    tmp_path / "shot.png"))


def test_task_prompt_forbids_submitting_and_names_the_resume(tmp_path):
    task = fill.TASK.format(resume=tmp_path / "resume.pdf", applicant="Jane")
    assert "Do not submit" in task
    assert "resume.pdf" in task
    assert "Never invent" in task


def test_profile_mode_uses_a_dedicated_persistent_directory(tmp_path, monkeypatch):
    """Never the everyday profile: Chrome will not share it with a running instance."""
    monkeypatch.delenv("AUTOPILOT_CDP_URL", raising=False)
    monkeypatch.setenv("AUTOPILOT_CHROME_PROFILE", str(tmp_path / "profile"))
    seen = {}

    def FakeBrowser(**kwargs):
        seen.update(kwargs)
        return "browser"

    fill.build_browser(FakeBrowser)
    assert seen["user_data_dir"] == str(tmp_path / "profile")
    assert (tmp_path / "profile").is_dir(), "the directory must exist before launch"
    assert "cdp_url" not in seen


def test_the_default_profile_is_not_the_users_own_chrome():
    everyday = Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
    assert fill.DEFAULT_PROFILE != everyday
    assert "job-autopilot" in str(fill.DEFAULT_PROFILE)


def test_attach_mode_connects_instead_of_launching(monkeypatch):
    monkeypatch.setenv("AUTOPILOT_CDP_URL", "http://127.0.0.1:9222")
    seen = {}

    def FakeBrowser(**kwargs):
        seen.update(kwargs)
        return "browser"

    fill.build_browser(FakeBrowser)
    assert seen["cdp_url"] == "http://127.0.0.1:9222"
    assert "user_data_dir" not in seen, "attach mode must not touch a profile directory"


@pytest.mark.parametrize("model,expected", [
    ("z-ai/glm-5v-turbo", True),
    ("z-ai/glm-4.6v", True),
    ("qwen/qwen3-vl-235b", True),
    ("z-ai/glm-5.3", False),
    ("deepseek/deepseek-v4-pro", False),
])
def test_vision_is_only_enabled_for_a_model_that_accepts_images(model, expected, monkeypatch):
    """A text-only model 404s on every screenshot and the run fills nothing."""
    monkeypatch.delenv("AUTOPILOT_VISION", raising=False)
    assert fill.uses_vision(model) is expected


def test_vision_can_be_forced_either_way(monkeypatch):
    monkeypatch.setenv("AUTOPILOT_VISION", "0")
    assert fill.uses_vision("z-ai/glm-5v-turbo") is False
    monkeypatch.setenv("AUTOPILOT_VISION", "1")
    assert fill.uses_vision("z-ai/glm-5.3") is True


def test_the_default_browser_model_accepts_images():
    assert fill.uses_vision(fill.DEFAULT_BROWSER_MODEL)


def test_result_summary_names_errors_and_blocks():
    result = fill.FillResult(ok=False, steps=6, screenshot=None, notes="",
                             blocked_attempts=1, errors=["404 no image support"])
    summary = result.summary()
    assert "6 step(s)" in summary and "1 submit attempt" in summary and "1 error" in summary


def test_the_browser_is_never_killed_after_a_fill():
    """Checkpoint 2 is a human reading the filled form; closing it defeats that."""
    import inspect

    source = inspect.getsource(fill)
    assert "browser.kill()" not in source, "kill() closes the window the human needs"
    assert "await browser.stop()" in inspect.getsource(fill._detach)


def test_detach_stops_the_session_without_closing_the_window():
    calls = []

    class FakeBrowser:
        async def stop(self):
            calls.append("stop")

        async def kill(self):
            calls.append("kill")

    asyncio.run(fill._detach(FakeBrowser()))
    assert calls == ["stop"]


def test_detach_swallows_a_failure_rather_than_closing():
    class Broken:
        async def stop(self):
            raise RuntimeError("cdp gone")

    asyncio.run(fill._detach(Broken()))  # must not raise
