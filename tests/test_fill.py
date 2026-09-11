"""The fill loop's wiring: the guard actually intercepts, and node reading works."""

import asyncio
import types

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
