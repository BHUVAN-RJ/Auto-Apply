"""The Prompts tab, the Workshop, and first-run setup.

- `/prompts`: every system prompt, the person's version of it, its stock
  text, its history, and a pending update conflict when there is one.
- `/workshop`: requests in words, turned into prompt edits by
  `tailor/workshop.py`; applied only on the person's click.
- `/setup`: what is still missing before the first job (the OpenRouter
  key, the master resume, Chrome), and the key itself. The key is typed
  into the page, checked against OpenRouter, and written to the data
  directory's `.env`; it is never sent anywhere else, and never through
  Claude Code's transcript.
"""

from __future__ import annotations

import os
import re

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import paths
from browser import chrome
from tailor import prompts, tailor, workshop

router = APIRouter()

KEY_CHECK_URL = "https://openrouter.ai/api/v1/key"
KEY_SHAPE = re.compile(r"^sk-or-[A-Za-z0-9_-]{20,}$")


class PromptText(BaseModel):
    text: str


class Version(BaseModel):
    version: str


class WorkshopRequest(BaseModel):
    request: str


class KeyRequest(BaseModel):
    key: str


def _prompt_or_404(fn, *args):
    try:
        return fn(*args)
    except prompts.PromptError as exc:
        raise HTTPException(404, str(exc)) from None


# ------------------------------------------------------------ prompts --


@router.get("/prompts")
def list_prompts() -> dict:
    return {"prompts": prompts.listing()}


@router.get("/prompts/{name}")
def get_prompt(name: str) -> dict:
    return _prompt_or_404(prompts.detail, name)


@router.post("/prompts/{name}")
def save_prompt(name: str, body: PromptText) -> dict:
    if not body.text.strip():
        raise HTTPException(400, "a prompt cannot be empty; Reset brings the stock text back")
    return _prompt_or_404(prompts.save, name, body.text)


@router.post("/prompts/{name}/reset")
def reset_prompt(name: str) -> dict:
    return _prompt_or_404(prompts.reset, name)


@router.post("/prompts/{name}/restore")
def restore_prompt(name: str, body: Version) -> dict:
    return _prompt_or_404(prompts.restore, name, body.version)


# ----------------------------------------------------------- workshop --


def _workshop(fn, *args):
    try:
        return fn(*args)
    except workshop.WorkshopError as exc:
        raise HTTPException(400, str(exc)) from None
    except workshop.llm.LLMError as exc:
        raise HTTPException(502, str(exc)) from None


@router.get("/workshop")
def get_workshop() -> dict:
    return {"proposals": workshop.proposals()[-50:],
            "unseen": len(workshop.unseen()),
            "learn_every": workshop.LEARN_EVERY}


@router.post("/workshop")
def ask_workshop(body: WorkshopRequest) -> dict:
    return _workshop(workshop.propose, body.request)


@router.post("/workshop/learn")
def learn_now() -> dict:
    proposal = _workshop(workshop.learn)
    return {"proposal": proposal}


@router.post("/workshop/{pid}/apply")
def apply_proposal(pid: str) -> dict:
    return _workshop(workshop.apply, pid)


@router.post("/workshop/{pid}/dismiss")
def dismiss_proposal(pid: str) -> dict:
    return _workshop(workshop.dismiss, pid)


# -------------------------------------------------------------- setup --


def key_is_set() -> bool:
    return bool(os.environ.get("OPENROUTER_API_KEY", "").strip())


@router.get("/setup")
def setup_status() -> dict:
    """What the first-run card lists. Each item is done or not; nothing
    here blocks the page."""
    resume = paths.BASE / "resume.tex"
    problems = tailor.master_problems(resume.read_text(errors="replace")) if resume.exists() else []
    return {
        "home": str(paths.HOME),
        "key": key_is_set(),
        "resume": resume.exists() and not problems,
        "resume_problems": problems,
        "resume_path": str(resume),
        "chrome": chrome.find_browser() is not None,
    }


def write_env(path, name: str, value: str) -> None:
    """Set one variable in a .env file, keeping every other line."""
    lines = path.read_text().splitlines() if path.exists() else []
    line = f"{name}={value}"
    for i, existing in enumerate(lines):
        if re.match(rf"^\s*#?\s*{re.escape(name)}=", existing):
            lines[i] = line
            break
    else:
        lines.append(line)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    os.chmod(path, 0o600)


def check_key(key: str) -> None:
    """Raises HTTPException unless OpenRouter accepts the key."""
    try:
        response = httpx.get(KEY_CHECK_URL, headers={"Authorization": f"Bearer {key}"},
                             timeout=15)
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"could not reach OpenRouter: {exc}") from None
    if response.status_code in (401, 403):
        raise HTTPException(400, "OpenRouter refused this key. Copy it again from "
                                 "openrouter.ai/settings/keys.")
    if response.status_code >= 400:
        raise HTTPException(502, f"OpenRouter answered {response.status_code}")


@router.post("/setup/key")
def set_key(body: KeyRequest) -> dict:
    key = body.key.strip()
    if not KEY_SHAPE.match(key):
        raise HTTPException(400, "That does not look like an OpenRouter key; they start "
                                 "with sk-or-.")
    check_key(key)
    write_env(paths.ENV_FILE, "OPENROUTER_API_KEY", key)
    # The server and every script it starts from now on see it at once.
    os.environ["OPENROUTER_API_KEY"] = key
    return setup_status()
