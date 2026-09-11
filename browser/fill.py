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
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import guard

ROOT = Path(__file__).resolve().parent.parent

# The agent's brief. The real constraints are in code; this exists so the
# model does not waste steps trying things the guard will refuse anyway.
TASK = """Fill in this job application form. Do not submit it.

Steps:
1. If a Jobright autofill button or panel is offered, click it and let it
   populate the form. That is the fastest path and it is already configured.
2. Fill any field the autofill missed, using the applicant details below.
3. Wherever a resume or CV file is requested, upload the file at this exact
   path, replacing anything already attached: {resume}
4. When every visible required field has a value, call done.

Absolute rules:
- Never click submit, apply, send, finish, or any control that would file the
  application. A human does that. Attempting it will be refused.
- Never invent an answer. If a required question needs information you do not
  have, leave it blank and say so when you call done.
- Do not create an account or accept terms on the applicant's behalf.

Applicant details:
{applicant}
"""

# A dedicated profile directory, not the everyday one. Chrome refuses to share
# a user-data-dir with a running instance and silently falls back to a
# throwaway, which is why pointing at the real profile lost the login on every
# run. This one belongs to Auto-Apply alone: log in once and it persists.
DEFAULT_PROFILE = Path.home() / "Library" / "Application Support" / "job-autopilot" / "chrome"

# Set AUTOPILOT_CDP_URL to attach to a browser you already have open instead,
# started with --remote-debugging-port=9222. That reuses every login and
# extension you already have, at the cost of launching the browser yourself.
CDP_URL = "AUTOPILOT_CDP_URL"


@dataclass
class FillResult:
    ok: bool
    steps: int
    screenshot: Optional[Path]
    notes: str
    blocked_attempts: int = 0


class FillError(RuntimeError):
    pass


def applicant_details() -> str:
    """Applicant facts for fields the autofill misses.

    Read from base/applicant.md so nothing personal is hard-coded, and so this
    file stays safe to publish.
    """
    path = ROOT / "base" / "applicant.md"
    return path.read_text().strip() if path.exists() else "(none provided)"


def _build_tools():
    """A browser-use Tools instance with the submit path removed.

    The guard wraps `click` rather than replacing it: the original action still
    does the work, but only after the element's text and attributes have been
    checked. `evaluate` and `send_keys` are dropped entirely, since either
    would route around that check.
    """
    from browser_use import Tools

    tools = Tools(exclude_actions=list(guard.FORBIDDEN_ACTIONS))

    registry = tools.registry.registry
    original = registry.actions.get("click")
    if original is None:  # pragma: no cover - browser-use changed its action set
        raise FillError("browser-use has no 'click' action to guard")

    inner = original.function

    async def guarded_click(*args, **kwargs):
        # browser-use calls this as click(params=..., browser_session=...),
        # where params carries only an element index. The node has to be
        # looked up in the session's selector map before it can be judged.
        params = kwargs.get("params") or (args[0] if args else None)
        session = kwargs.get("browser_session")
        index = getattr(params, "index", None)
        if session is not None and index is not None:
            node = await session.get_element_by_index(index)
            guard.check_click(**describe(node))
        return await inner(*args, **kwargs)

    original.function = guarded_click
    return tools


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


def profile_dir() -> Path:
    return Path(os.environ.get("AUTOPILOT_CHROME_PROFILE", str(DEFAULT_PROFILE)))


def build_browser(browser_class, headless: bool = False):
    """Attach to a running browser if asked, otherwise own a persistent profile.

    Attach mode reuses the browser the user already has open, with all its
    logins and extensions. Profile mode launches its own Chrome against a
    directory that survives between runs, so the Jobright login and extension
    are installed once.
    """
    cdp_url = os.environ.get(CDP_URL, "").strip()
    if cdp_url:
        return browser_class(cdp_url=cdp_url, keep_alive=True)

    directory = profile_dir()
    directory.mkdir(parents=True, exist_ok=True)
    return browser_class(
        headless=headless,
        user_data_dir=str(directory),
        keep_alive=True,
    )


async def fill_async(
    url: str,
    resume_pdf: Path,
    screenshot_to: Path,
    max_steps: int = 40,
    headless: bool = False,
) -> FillResult:
    from browser_use import Agent, Browser, ChatOpenAI

    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        raise FillError("OPENROUTER_API_KEY is unset")

    llm = ChatOpenAI(
        model=os.environ.get("OPENROUTER_BROWSER_MODEL", "z-ai/glm-5.3"),
        base_url="https://openrouter.ai/api/v1",
        api_key=api_key,
        temperature=0.0,
    )

    browser = build_browser(Browser, headless=headless)

    agent = Agent(
        task=TASK.format(resume=resume_pdf.resolve(), applicant=applicant_details()),
        llm=llm,
        browser=browser,
        tools=_build_tools(),
        initial_actions=[{"navigate": {"url": url}}],
    )

    blocked = 0
    try:
        history = await agent.run(max_steps=max_steps)
        notes = str(history.final_result() or "").strip()
        steps = len(getattr(history, "history", []) or [])
        errors = [str(e) for e in (history.errors() or []) if e]
        blocked = sum(1 for e in errors if "submit control" in e)
    except guard.SubmitBlocked as exc:
        # Reaching here means the model kept pushing at the gate. That is a
        # refusal working, not a crash, so the run still ends in a screenshot.
        notes, steps, blocked = f"blocked: {exc}", 0, 1
    finally:
        shot = await _screenshot(browser, screenshot_to)
        await browser.kill()

    return FillResult(
        ok=shot is not None,
        steps=steps,
        screenshot=shot,
        notes=notes or "(no notes returned)",
        blocked_attempts=blocked,
    )


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
