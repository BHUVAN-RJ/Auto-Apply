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
    task = fill.TASK.format(resume=tmp_path / "resume.pdf", resume_name="resume.pdf",
                            cover_letter_step="", location="LA", applicant="Jane")
    assert "Do not submit" in task
    assert "resume.pdf" in task
    assert "Never invent" in task


def test_task_prompt_replaces_the_autofilled_resume_after_autofill_finishes(tmp_path):
    task = fill.TASK.format(resume=tmp_path / "resume.pdf", resume_name="resume.pdf",
                            cover_letter_step="", location="LA", applicant="Jane")
    assert task.index("autofill has finished") < task.index("remove it first")
    assert task.index("remove it first") < task.index("upload_file")


def test_the_resume_path_is_declared_to_browser_use():
    """upload_file refuses undeclared paths; the first real run proved it."""
    import inspect

    source = inspect.getsource(fill.fill_async)
    assert "available_file_paths=[str(p.resolve()) for p in (resume_pdf, cover_letter_pdf) if p]" in source


class FakeAction:
    def __init__(self, **payload):
        self.payload = payload

    def model_dump(self, exclude_none=True):
        return self.payload


def fake_history(steps):
    return types.SimpleNamespace(history=[
        types.SimpleNamespace(
            model_output=types.SimpleNamespace(action=[FakeAction(**a) for a, _ in step]),
            result=[types.SimpleNamespace(**r) for _, r in step],
        )
        for step in steps
    ])


def test_resume_uploaded_reads_the_action_log_not_the_done_text(tmp_path):
    resume = tmp_path / "resume.pdf"
    ok = {"error": None, "extracted_content": "Successfully uploaded file to index 3"}
    refused = {"error": "File path is not available", "extracted_content": None}
    other = {"error": None, "extracted_content": "Successfully uploaded file to index 3"}

    assert fill.resume_uploaded(fake_history([
        [({"upload_file": {"index": 3, "path": str(resume.resolve())}}, ok)],
    ]), resume)
    assert not fill.resume_uploaded(fake_history([
        [({"upload_file": {"index": 3, "path": str(resume.resolve())}}, refused)],
        [({"done": {"text": "the resume is attached", "success": True}}, ok)],
    ]), resume)
    assert not fill.resume_uploaded(fake_history([
        [({"upload_file": {"index": 3, "path": "/somewhere/else.pdf"}}, other)],
    ]), resume)
    assert not fill.resume_uploaded(types.SimpleNamespace(history=[]), resume)
    assert not fill.resume_uploaded(types.SimpleNamespace(history=[
        types.SimpleNamespace(model_output=None, result=[]),
    ]), resume)


def test_a_done_run_without_the_upload_is_not_ok():
    result = fill.FillResult(ok=False, steps=8, screenshot=None, notes="", done=True,
                             resume_uploaded=False)
    assert "resume NOT replaced" in result.summary()
    assert "resume replaced" in fill.FillResult(ok=True, steps=8, screenshot=None,
                                                notes="", resume_uploaded=True).summary()


def test_profile_mode_launches_our_own_chrome_and_attaches(tmp_path, monkeypatch):
    """browser-use must never launch the browser: it kills what it launches."""
    from browser import chrome

    monkeypatch.delenv("AUTOPILOT_CDP_URL", raising=False)
    monkeypatch.setenv("AUTOPILOT_CHROME_PROFILE", str(tmp_path / "profile"))
    launched = {}

    def fake_ensure(directory, port_number=None, headless=False):
        launched["directory"] = directory
        return "http://127.0.0.1:9333"

    monkeypatch.setattr(chrome, "ensure", fake_ensure)
    seen = {}

    def FakeBrowser(**kwargs):
        seen.update(kwargs)
        return "browser"

    fill.build_browser(FakeBrowser)
    assert launched["directory"] == tmp_path / "profile"
    assert seen == {"cdp_url": "http://127.0.0.1:9333", "keep_alive": True}
    assert "user_data_dir" not in seen, "a user_data_dir makes browser-use own, and kill, the process"


def test_build_browser_never_hands_browser_use_a_profile_to_launch():
    import inspect

    source = inspect.getsource(fill.build_browser)
    assert "user_data_dir" not in source
    assert "cdp_url=" in source


def test_each_fill_opens_in_a_new_tab():
    """The browser is shared across runs; the last form may still be on screen."""
    import inspect

    assert '"new_tab": True' in inspect.getsource(fill.fill_async)


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


class FakeUploadSession(FakeSession):
    def __init__(self, node, file_input):
        super().__init__(node)
        self.file_input = file_input

    def find_file_input_near_element(self, node):
        return self.file_input


def test_upload_is_refused_on_a_cover_letter_input():
    """Greenhouse: id=cover_letter next to id=resume. The tailored resume
    landed on the first one on a real run."""
    cover = FakeNode("", {"id": "cover_letter", "type": "file"})
    with pytest.raises(fill.UploadMisdirected, match="cover letter"):
        call_real_pinned_upload(FakeUploadSession(FakeNode("Attach"), file_input=cover))


def test_upload_passes_on_a_resume_input():
    resume = FakeNode("", {"id": "resume", "type": "file"})
    try:
        call_real_pinned_upload(FakeUploadSession(FakeNode("Attach"), file_input=resume))
    except fill.UploadMisdirected:
        pytest.fail("the resume input must be accepted")
    except Exception:
        pass


def call_real_action(name, node):
    tools = fill._build_tools()
    action = tools.registry.registry.actions[name].function
    params = types.SimpleNamespace(index=1, text="H-1B", clear=True)
    return asyncio.run(action(params=params, browser_session=FakeSession(node)))


VISA_QUESTION = "Will you now or in the future require sponsorship for employment visa status?"


@pytest.mark.parametrize("name", ["input", "select_dropdown", "click"])
def test_visa_fields_are_refused_for_every_way_of_answering(name):
    node = FakeNode(text="", attributes={"id": "question_1"}, ax_name=VISA_QUESTION)
    with pytest.raises(guard.ProtectedField):
        call_real_action(name, node)


@pytest.mark.parametrize("name", ["input", "select_dropdown", "click"])
def test_ordinary_fields_are_not_refused(name):
    node = FakeNode(text="", attributes={"id": "question_2"}, ax_name="Current Company")
    try:
        call_real_action(name, node)
    except guard.ProtectedField:
        pytest.fail("an ordinary field must not be refused")
    except Exception:
        pass  # the real action rejecting a fake session is expected


def test_a_radio_option_is_judged_by_the_question_above_it():
    """A radio reads "Yes"; the visa question is in the fieldset around it."""
    fieldset = FakeNode(text=None, attributes={"id": "work-authorization-group"})
    wrapper = FakeNode(text=None, attributes={})
    wrapper.parent_node = fieldset
    radio = FakeNode(text="", attributes={"type": "radio", "value": "yes"}, ax_name="Yes")
    radio.parent_node = wrapper
    assert "ancestor1_id" in fill.context(radio)
    with pytest.raises(guard.ProtectedField):
        call_real_action("click", radio)


def test_context_stops_at_the_root_and_drops_long_text():
    lone = FakeNode("Yes")
    assert fill.context(lone) == {}
    parent = FakeNode("visa " * 200)
    child = FakeNode("Yes")
    child.parent_node = parent
    assert fill.context(child) == {}


def test_task_prompt_tells_the_agent_to_leave_visa_questions_alone(tmp_path):
    task = fill.TASK.format(resume=tmp_path / "r.pdf", resume_name="r.pdf",
                            cover_letter_step="", location="LA", applicant="Jane")
    assert "visa" in task and "Never touch" in task


def call_real_pinned_upload(session):
    tools = fill._build_tools()
    pinned = tools.registry.registry.actions["upload_file"].function
    params = types.SimpleNamespace(index=1, path="/tmp/resume.pdf")
    return asyncio.run(pinned(params=params, browser_session=session))


def test_upload_is_refused_when_no_file_input_is_near_the_target():
    """browser-use would otherwise fall back to the nearest input on the page,
    which on Greenhouse was the cover letter slot."""
    with pytest.raises(fill.UploadMisdirected):
        call_real_pinned_upload(FakeUploadSession(FakeNode("Attach"), file_input=None))


def test_upload_reaches_browser_use_when_a_file_input_is_near():
    try:
        call_real_pinned_upload(FakeUploadSession(FakeNode("Attach"), file_input=object()))
    except fill.UploadMisdirected:
        pytest.fail("an upload aimed at a real file input must pass through")
    except Exception:
        pass  # the real upload rejecting a fake session is the expected outcome


def test_the_real_upload_action_is_wrapped_not_replaced():
    import inspect

    source = inspect.getsource(fill._build_tools)
    assert 'wrap("upload_file", upload)' in source
    assert "await inner(" in source


def test_the_cover_letter_step_only_appears_when_there_is_a_letter(tmp_path):
    with_letter = fill.TASK.format(
        resume=tmp_path / "r.pdf", resume_name="r.pdf", applicant="Jane", location="LA",
        cover_letter_step=fill.COVER_LETTER_STEP.format(cover_letter=tmp_path / "cl.pdf"))
    assert "cl.pdf" in with_letter and "cover letter upload" in with_letter
    assert "leave it empty" in with_letter, "a text-box cover letter is never typed"
    without = fill.TASK.format(resume=tmp_path / "r.pdf", resume_name="r.pdf",
                               applicant="Jane", location="LA", cover_letter_step="")
    assert "cover letter upload" not in without


def call_pinned_upload_with(session, path, resume, cover):
    tools = fill._build_tools(resume, cover)
    pinned = tools.registry.registry.actions["upload_file"].function
    params = types.SimpleNamespace(index=1, path=str(path))
    return asyncio.run(pinned(params=params, browser_session=session))


def test_the_cover_letter_goes_only_on_a_cover_letter_input(tmp_path):
    resume, cover = tmp_path / "R.pdf", tmp_path / "CL.pdf"
    resume_input = FakeNode("", {"id": "resume", "type": "file"})
    cover_input = FakeNode("", {"id": "cover_letter", "type": "file"})

    with pytest.raises(fill.UploadMisdirected, match="not a cover letter input"):
        call_pinned_upload_with(FakeUploadSession(FakeNode("Attach"), resume_input),
                                cover.resolve(), resume, cover)
    with pytest.raises(fill.UploadMisdirected, match="cover letter input"):
        call_pinned_upload_with(FakeUploadSession(FakeNode("Attach"), cover_input),
                                resume.resolve(), resume, cover)
    for path, target in ((cover.resolve(), cover_input), (resume.resolve(), resume_input)):
        try:
            call_pinned_upload_with(FakeUploadSession(FakeNode("Attach"), target), path, resume, cover)
        except fill.UploadMisdirected:
            pytest.fail(f"{path.name} on {target.attributes['id']} must be allowed")
        except Exception:
            pass


def test_summary_reports_the_cover_letter():
    result = fill.FillResult(ok=True, steps=3, screenshot=None, notes="",
                             resume_uploaded=True, cover_letter_uploaded=True)
    assert "cover letter attached" in result.summary()
    assert "cover letter" not in fill.FillResult(ok=True, steps=3, screenshot=None, notes="",
                                                 resume_uploaded=True).summary()


def test_the_task_pins_every_location_field_to_the_applicants_city(monkeypatch, tmp_path):
    monkeypatch.delenv(fill.LOCATION_ENV, raising=False)
    task = fill.TASK.format(resume=tmp_path / "r.pdf", resume_name="r.pdf", applicant="Jane",
                            location=fill.location(), cover_letter_step="")
    assert "Los Angeles, California, United States" in task
    assert task.index("autofill has finished") < task.index("Los Angeles")
    assert "location: <what the field shows>" in task
    monkeypatch.setenv(fill.LOCATION_ENV, "Austin, Texas, United States")
    assert fill.location() == "Austin, Texas, United States"


def test_answer_question_is_only_offered_when_an_answerer_exists():
    assert "answer_question" not in fill._build_tools().registry.registry.actions
    tools = fill._build_tools(answerer=lambda q: "an answer")
    assert "answer_question" in tools.registry.registry.actions


def test_answer_question_returns_the_answerers_text():
    tools = fill._build_tools(answerer=lambda q: f"answer to {q}")
    action = tools.registry.registry.actions["answer_question"]
    params = action.param_model(question="Why here?")
    result = asyncio.run(action.function(params=params))
    assert "answer to Why here?" in result.extracted_content


def test_task_sends_open_questions_to_answer_question(tmp_path):
    task = fill.TASK.format(resume=tmp_path / "r.pdf", resume_name="r.pdf", applicant="Jane",
                            location="LA", cover_letter_step="")
    assert "answer_question" in task and "Never compose such an answer yourself" in task
