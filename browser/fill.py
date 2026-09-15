"""Checkpoint 2: fill the application form, screenshot it, and stop.

Drives the user's real Chrome profile so existing logins and the installed
autofill extension are available — reimplementing per-ATS field mapping is the
largest time sink in a project like this, and the extension already knows those
layouts.

The run is bounded three ways. The agent's click action is wrapped by the
guard, the actions that could submit without a click are removed from its
vocabulary, and the terminal state is always a screenshot plus `status: filled`.
Nothing here can reach a submit button.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import chrome, guard

ROOT = Path(__file__).resolve().parent.parent

# The agent's brief. The real constraints are in code; this exists so the
# model does not waste steps trying things the guard will refuse anyway.
TASK = """Fill in this job application form. Do not submit it.

Steps:
1. If a Jobright autofill button or panel is offered, click it and let it
   populate the form. That is the fastest path and it is already configured.
   Wait until the panel reports that autofill has finished before going on;
   it is still changing fields, and may attach its own resume, until then.
2. Replace the resume. Autofill attaches a generic resume; this application
   needs the tailored one. In the resume / CV section:
   a. If a file is already attached, remove it first: click its X, remove,
      delete, or replace control. That control is allowed.
   b. Then upload the file at this exact path with the upload_file action,
      pointing it at the resume upload control: {resume}
   c. Check the section now shows a file named {resume_name}. If it does not,
      the upload did not take: try again on the file input itself.
   Do the same for any other field that asks for a resume or CV. Do not
   attach it as a cover letter; if {resume_name} appears under a cover letter
   field, remove it there with that field's X.
{cover_letter_step}3. Location. Autofill sometimes writes the wrong city or country (it has
   entered "Delhi, India"). Every location, city, address, state, or country
   field must read: {location}
   Check each one after autofill finishes. If it shows anything else, clear
   it and type the correct value; if it is a dropdown or autocomplete, pick
   the matching entry (city first, then state, then country). The country
   is always United States and the phone country code is +1.
4. Fill any field the autofill missed, using the applicant details below,
   except questions about visa, sponsorship, work authorisation, OPT / CPT,
   citizenship, or immigration status. Never touch those: do not type in
   them, do not open or pick from their dropdowns, whether they are empty or
   already filled. The action will be refused. List them when you call done.
5. Free-form questions. For any text box that asks an open question ("Why
   do you want to work here?", "What are you looking for in your next
   role?", "Tell us about a project"), call answer_question with the
   question text exactly as it appears, then type the answer it returns
   into that box with input. Never compose such an answer yourself, and
   never leave one of these empty when answer_question can answer it.
   Visa and work-authorisation questions are refused and stay blank.
6. When every other visible required field has a value and the tailored
   resume is attached, call done. The done text is four short lines, no
   prose: `resume: replaced` or `resume: NOT replaced`; `cover letter:
   attached` / `no field` / `skipped`; `location: <what the field shows>`;
   `left blank: <field, field>` or `left blank: none`.

Absolute rules:
- Never click submit, apply, send, finish, or any control that would file the
  application. A human does that. Attempting it will be refused.
- Never invent an answer. If a required question needs information you do not
  have, leave it blank and say so when you call done.
- Do not create an account or accept terms on the applicant's behalf.
- Never report the resume as attached unless you uploaded it yourself in this
  session and saw its name appear.

Applicant details:
{applicant}
"""

COVER_LETTER_STEP = """   Then the cover letter. If the form has a cover letter upload, attach the
   file at this exact path to it with upload_file, removing anything already
   there first: {cover_letter}
   If the form only offers a text box for the cover letter, leave it empty.
   If there is no cover letter field at all, skip this.
"""

# A dedicated profile directory, not the everyday one. Chrome refuses to share
# a user-data-dir with a running instance and silently falls back to a
# throwaway, which is why pointing at the real profile lost the login on every
# run. This one belongs to Auto-Apply alone: log in once and it persists.
DEFAULT_PROFILE = Path.home() / "Library" / "Application Support" / "job-autopilot" / "chrome"

# Set AUTOPILOT_CDP_URL to attach to a browser you already have open instead,
# started with --remote-debugging-port=9222. That reuses every login and
# extension you already have, at the cost of launching the browser yourself.
# Without it, browser/chrome.py launches Chrome on the profile above and hands
# over a CDP URL the same way, so browser-use never owns the process either
# way. See that module for why ownership is the whole problem.
CDP_URL = "AUTOPILOT_CDP_URL"

# Where the applicant is, as every location field on every form must read.
# Autofill has written the wrong country before; the agent corrects to this.
LOCATION_ENV = "AUTOPILOT_LOCATION"
DEFAULT_LOCATION = "Los Angeles, California, United States"


def location() -> str:
    return os.environ.get(LOCATION_ENV, "").strip() or DEFAULT_LOCATION

# browser-use sends a screenshot to the model on every step, so the browser
# model has to accept images. A text-only model returns 404 on every step and
# the run fails having filled nothing.
# Gemini Flash rather than a GLM vision model: glm-5v-turbo accepts images but
# is unreliable at browser-use's action schema, emitting {"click": [3713]} where
# {"index": 3713} is required. Every step then fails validation and the run
# explores until it runs out of steps. Structured-output reliability matters
# more here than price.
DEFAULT_BROWSER_MODEL = "google/gemini-3.1-flash-lite"

# Substrings marking a model as able to accept images. Checked rather than
# assumed, because picking a text-only model here is a silent, total failure.
VISION_MODEL_MARKERS = (
    "-5v", "-4.6v", "vl", "vision", "gpt-4o", "gpt-5", "gemini", "claude",
    "gemma", "nova", "pixtral", "qwen3.5", "qwen3.6", "qwen3.7", "qwen3.8", "glm-5.3-flash",
)


@dataclass
class FillResult:
    ok: bool
    steps: int
    screenshot: Optional[Path]
    notes: str
    blocked_attempts: int = 0
    errors: list[str] = field(default_factory=list)
    done: bool = False
    resume_uploaded: bool = False
    cover_letter_uploaded: bool = False

    def summary(self) -> str:
        parts = [f"{self.steps} step(s)"]
        parts.append("resume replaced" if self.resume_uploaded else "resume NOT replaced")
        if self.cover_letter_uploaded:
            parts.append("cover letter attached")
        if self.blocked_attempts:
            parts.append(f"blocked {self.blocked_attempts} submit attempt(s)")
        if self.errors:
            parts.append(f"{len(self.errors)} error(s)")
        return "; ".join(parts)


class FillError(RuntimeError):
    pass


def resume_uploaded(history, resume_pdf: Path) -> bool:
    """Whether the agent's own upload of this file succeeded at least once.

    The model has reported "the resume is attached" after its upload was
    refused — the attachment it saw was Jobright's generic one. So the claim
    is not taken from the done text; it is read off the action log, matching
    an upload_file of this exact path to a result with no error.
    """
    wanted = str(resume_pdf.resolve())
    for step in getattr(history, "history", []) or []:
        output = getattr(step, "model_output", None)
        actions = getattr(output, "action", None) or []
        results = getattr(step, "result", None) or []
        for action, result in zip(actions, results):
            dump = action.model_dump(exclude_none=True) if hasattr(action, "model_dump") else dict(action)
            params = dump.get("upload_file")
            if not params or params.get("path") != wanted:
                continue
            if getattr(result, "error", None):
                continue
            if "uploaded" in str(getattr(result, "extracted_content", "") or "").lower():
                return True
    return False


def applicant_details() -> str:
    """Applicant facts for fields the autofill misses.

    Read from base/applicant.md so nothing personal is hard-coded, and so this
    file stays safe to publish.
    """
    path = ROOT / "base" / "applicant.md"
    return path.read_text().strip() if path.exists() else "(none provided)"


def _build_tools(resume_pdf: Optional[Path] = None, cover_letter_pdf: Optional[Path] = None,
                 answerer=None):
    """A browser-use Tools instance with the submit path removed.

    Each guarded action is wrapped rather than replaced: the original still
    does the work, but only after the target element has been looked up and
    judged. `evaluate` and `send_keys` are dropped entirely, since either
    would route around every check here.

    click        - refuses submit controls and visa / work-authorisation fields
    input        - refuses visa / work-authorisation fields
    select_dropdown - refuses visa / work-authorisation fields
    upload_file  - refuses a target with no file input of its own; refuses
                   the resume on a cover letter input and the cover letter
                   on anything but one
    """
    from browser_use import Tools

    tools = Tools(exclude_actions=list(guard.FORBIDDEN_ACTIONS))
    registry = tools.registry.registry

    def wrap(name: str, check):
        original = registry.actions.get(name)
        if original is None:  # pragma: no cover - browser-use changed its action set
            raise FillError(f"browser-use has no {name!r} action to guard")
        inner = original.function

        async def guarded(*args, **kwargs):
            # browser-use calls actions as name(params=..., browser_session=...),
            # where params carries only an element index. The node has to be
            # looked up in the session's selector map before it can be judged.
            params = kwargs.get("params") or (args[0] if args else None)
            session = kwargs.get("browser_session")
            index = getattr(params, "index", None)
            if session is not None and index is not None:
                node = await session.get_element_by_index(index)
                check(node, session, index, params)
            return await inner(*args, **kwargs)

        original.function = guarded

    def click(node, session, index, params):
        guard.check_click(**describe(node))
        guard.check_protected(**context(node))

    def protected(node, session, index, params):
        guard.check_protected(**describe(node))
        guard.check_protected(**context(node))

    def upload(node, session, index, params):
        # browser-use's upload_file has a fallback: when no file input sits
        # near the chosen element it uploads to whichever file input is
        # closest to the scroll position. On a Greenhouse form that put the
        # resume into the cover letter slot. Refuse instead, so the file only
        # ever lands on the input the model actually pointed at.
        finder = getattr(session, "find_file_input_near_element", None)
        if node is None or not callable(finder):
            return
        file_input = finder(node)
        if file_input is None:
            raise UploadMisdirected(
                f"element {index} has no file input near it; point upload_file at "
                "the resume section's own file input or its attach control"
            )
        # Greenhouse names its inputs `resume` and `cover_letter`. Each file
        # goes on its own input and never the other's, whatever the model
        # thought it was aiming at. Judged by the file being uploaded, so a
        # form with only a resume slot never receives the letter.
        fields = describe(file_input)
        is_cover_input = guard.describes_cover_letter(*fields.values())
        path = str(getattr(params, "path", "") or "")
        is_cover_file = bool(cover_letter_pdf) and path == str(cover_letter_pdf.resolve())
        if is_cover_file and not is_cover_input:
            raise UploadMisdirected(
                f"element {index} is not a cover letter input ({guard._describe(fields)}); "
                "the cover letter goes only on a cover letter upload, or nowhere"
            )
        if not is_cover_file and is_cover_input:
            raise UploadMisdirected(
                f"element {index} feeds the cover letter input "
                f"({guard._describe(fields)}); upload the resume to the resume input"
            )

    wrap("click", click)
    wrap("input", protected)
    wrap("select_dropdown", protected)
    wrap("upload_file", upload)

    if answerer is not None:
        _register_answer_action(tools, answerer)
    return tools


def _register_answer_action(tools, answerer) -> None:
    """Give the agent one way to answer an open question: ask the tailor model.

    The browser model is picked for following the action schema, not for
    writing. It hands the question text over and types back what it gets.
    The call is synchronous (one HTTP round trip to the model), so it runs
    in a thread rather than stalling the browser's event loop.
    """
    from browser_use.agent.views import ActionResult

    @tools.registry.action(
        "Get the applicant's answer to a free-form question on the form, such as "
        "'Why do you want to work here?' or 'What are you looking for in your next "
        "role?'. Pass the question text exactly as shown. Then type the returned "
        "answer into that field with input. Never write such an answer yourself."
    )
    async def answer_question(question: str) -> ActionResult:
        text = await asyncio.to_thread(answerer, question)
        return ActionResult(
            extracted_content=f"Answer to {question[:60]!r}:\n{text}",
            long_term_memory=f"Got an answer for {question[:60]!r}; type it into that field.",
            include_extracted_content_only_once=True,
        )


class UploadMisdirected(RuntimeError):
    """upload_file aimed at an element with no file input of its own, or the wrong one."""


def context(node, levels: int = 4) -> dict:
    """Labels from the element's ancestors, for controls whose own text says nothing.

    A radio button reads "Yes"; the question it answers is in a fieldset a few
    levels up. Each ancestor's accessible name, aria-label, id, and a short
    slice of its text are collected so the guard can judge the group. Text
    longer than a label is dropped, since four levels up may be the whole form.
    """
    found: dict = {}
    current = node
    for level in range(levels):
        current = getattr(current, "parent_node", None)
        if current is None:
            break
        fields = describe(current)
        for key, value in fields.items():
            if value and len(str(value)) <= 400:
                found[f"ancestor{level}_{key}"] = value
    return found


def describe(node) -> dict:
    """Pull the identifying fields off a DOM node, whatever shape it has.

    browser-use has moved this structure between releases, so every field is
    read defensively; a field that cannot be read is simply not checked.
    """
    if node is None:
        return {}

    def attribute(name: str):
        attributes = getattr(node, "attributes", None)
        if isinstance(attributes, dict):
            return attributes.get(name)
        return None

    text = None
    for candidate in ("get_all_children_text", "get_meaningful_text_for_llm"):
        method = getattr(node, candidate, None)
        if callable(method):
            try:
                text = method()
                break
            except Exception:  # noqa: BLE001 - best effort only
                continue
    if not text:
        # Fall back to the accessibility name, which is what a screen reader
        # would announce and often the only label an icon button has.
        ax = getattr(node, "ax_node", None)
        text = getattr(ax, "name", None) if ax is not None else None

    return {
        "text": text or getattr(node, "node_value", None),
        "aria_label": attribute("aria-label"),
        "id": attribute("id"),
        "name": attribute("name"),
        "value": attribute("value"),
        "type": attribute("type"),
    }


def uses_vision(model: str) -> bool:
    """Whether to send screenshots to this model.

    AUTOPILOT_VISION forces it either way, for a model whose name does not
    advertise the capability.
    """
    override = os.environ.get("AUTOPILOT_VISION", "").strip().lower()
    if override in ("1", "true", "yes"):
        return True
    if override in ("0", "false", "no"):
        return False
    name = model.lower()
    return any(marker in name for marker in VISION_MODEL_MARKERS)


def profile_dir() -> Path:
    return Path(os.environ.get("AUTOPILOT_CHROME_PROFILE", str(DEFAULT_PROFILE)))


def build_browser(browser_class, headless: bool = False):
    """Attach to a running browser, ours or the user's. Never launch through browser-use.

    browser-use's local-browser watchdog kills the process it launched on
    every stop, `keep_alive` or not, which closed the window holding the
    completed form. Handing it a CDP URL makes the browser remote from its
    point of view, and remote browsers are only ever disconnected from.
    """
    cdp_url = os.environ.get(CDP_URL, "").strip()
    if not cdp_url:
        cdp_url = chrome.ensure(profile_dir(), headless=headless)
    return browser_class(cdp_url=cdp_url, keep_alive=True)


async def fill_async(
    url: str,
    resume_pdf: Path,
    screenshot_to: Path,
    max_steps: int = 40,
    headless: bool = False,
    cover_letter_pdf: Optional[Path] = None,
    answerer=None,
) -> FillResult:
    from browser_use import Agent, Browser, ChatOpenAI

    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        raise FillError("OPENROUTER_API_KEY is unset")

    model = os.environ.get("OPENROUTER_BROWSER_MODEL", DEFAULT_BROWSER_MODEL)
    llm = ChatOpenAI(
        model=model,
        base_url="https://openrouter.ai/api/v1",
        api_key=api_key,
        temperature=0.0,
    )

    browser = build_browser(Browser, headless=headless)

    agent = Agent(
        task=TASK.format(
            resume=resume_pdf.resolve(),
            resume_name=resume_pdf.name,
            location=location(),
            cover_letter_step=(
                COVER_LETTER_STEP.format(cover_letter=cover_letter_pdf.resolve())
                if cover_letter_pdf else ""
            ),
            applicant=applicant_details(),
        ),
        llm=llm,
        browser=browser,
        tools=_build_tools(resume_pdf, cover_letter_pdf, answerer),
        # upload_file refuses any path not declared here. Without it every
        # upload failed and the agent reported Jobright's generic resume as
        # the tailored one.
        available_file_paths=[str(p.resolve()) for p in (resume_pdf, cover_letter_pdf) if p],
        # A new tab each time: the browser is shared across runs, and the
        # previous fill may still be sitting in the tab the human is reading.
        initial_actions=[{"navigate": {"url": url, "new_tab": True}}],
        # Screenshots are sent to the model on every step. A text-only model
        # 404s on all of them and the whole run fails without filling
        # anything, so vision is only enabled for a model that can accept it.
        use_vision=uses_vision(model),
    )

    blocked, steps, done, uploaded, cover_uploaded = 0, 0, False, False, False
    errors: list[str] = []
    notes = ""
    try:
        history = await agent.run(max_steps=max_steps)
        notes = str(history.final_result() or "").strip()
        steps = len(getattr(history, "history", []) or [])
        errors = [str(e) for e in (history.errors() or []) if e]
        blocked = sum(1 for e in errors if "submit control" in e)
        done = bool(history.is_done())
        uploaded = resume_uploaded(history, resume_pdf)
        cover_uploaded = bool(cover_letter_pdf) and resume_uploaded(history, cover_letter_pdf)
    except guard.SubmitBlocked as exc:
        # Reaching here means the model kept pushing at the gate. That is a
        # refusal working, not a crash, so the run still ends in a screenshot.
        notes, blocked = f"blocked: {exc}", 1
    finally:
        shot = await _screenshot(browser, screenshot_to)
        # The window stays open. Checkpoint 2 is a human reading the filled
        # form and pressing submit themselves, which cannot happen if the
        # browser is closed the moment the agent stops.
        await _detach(browser)

    # A screenshot proves the browser was alive, not that the form was filled.
    # A run that never reached done, or that errored on every step other than
    # a refused submit, has filled nothing and must not be reported as filled.
    real_errors = [e for e in errors if "submit control" not in e]
    ok = done and not (real_errors and steps and len(real_errors) >= steps)
    # The whole point of tailoring is the file on the form. A fill that left
    # Jobright's generic resume attached is not a fill the human should submit.
    if ok and not uploaded:
        ok = False
        real_errors.insert(0, "the tailored resume was never uploaded; the form still has whatever autofill attached")

    return FillResult(
        ok=ok,
        steps=steps,
        screenshot=shot,
        notes=notes or "(no notes returned)",
        blocked_attempts=blocked,
        errors=real_errors,
        done=done,
        resume_uploaded=uploaded,
        cover_letter_uploaded=cover_uploaded,
    )


async def _detach(browser) -> None:
    """Leave the browser running, disconnecting only this session's control.

    Nothing here may close the window: the filled form is the artifact the
    human acts on. Any failure to detach cleanly is ignored, since a lingering
    connection is harmless next to a closed window.
    """
    # stop() ends this session and saves storage state. It leaves the browser
    # running only because browser-use did not launch it (see build_browser);
    # on a browser it launched itself, stop() kills the process regardless of
    # keep_alive. kill() would close the window in every case.
    try:
        await browser.stop()
    except Exception:  # noqa: BLE001 - a lingering connection beats a closed window
        pass


async def _screenshot(browser, path: Path) -> Optional[Path]:
    """Always attempt a screenshot; a failed one must not mask the run."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = await browser.take_screenshot(full_page=True)
    except Exception:  # noqa: BLE001
        return None
    if not data:
        return None
    if isinstance(data, str):
        import base64

        data = base64.b64decode(data)
    path.write_bytes(data)
    return path


def fill(url: str, resume_pdf: Path, screenshot_to: Path, **kwargs) -> FillResult:
    """Synchronous entry point for the pipeline."""
    return asyncio.run(fill_async(url, resume_pdf, screenshot_to, **kwargs))
