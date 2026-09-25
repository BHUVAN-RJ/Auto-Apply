"""Report a problem or request a feature: a prefilled GitHub issue.

The app holds no GitHub token and sends nothing anywhere by itself. It
builds the issue's URL (the template in `.github/ISSUE_TEMPLATE`, the
person's words, a diagnostics block) and opens it in the person's default
browser, where they are signed in to GitHub; they read it there and press
Submit, or do not.

The repository is public, so the diagnostics carry nothing about the
person: versions, which setup steps are done, how many jobs are in which
state, which application systems failed and the first line of each error
with addresses, links and long numbers taken out. The page shows the block
before anything opens, editable.
"""

from __future__ import annotations

import os
import platform
import re
import subprocess
from collections import Counter
from urllib.parse import quote, urlparse

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import paths
from tailor import prompts

from . import queue

router = APIRouter()

REPO = "https://github.com/BHUVAN-RJ/Auto-Apply"
TEMPLATES = {"problem": "problem.yml", "feature": "feature.yml"}
# GitHub refuses very long URLs; the words come first, the diagnostics are
# cut to fit.
MAX_URL = 7500
# Switches worth knowing when reading a report; none of them is a secret.
SWITCHES = ("AUTOPILOT_AGENT", "AUTOPILOT_FORM_FILL", "AUTOPILOT_DOCS_BY_CODE",
            "AUTOPILOT_AUTOFILL", "AUTOPILOT_AUTOFILL_BY_CODE", "AUTOPILOT_TEX_ENGINE",
            "OPENROUTER_TAILOR_MODEL", "OPENROUTER_SCREEN_MODEL")

EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
URL = re.compile(r"https?://[^\s)\"']+")
LONG_NUMBER = re.compile(r"\+?\d[\d ()-]{7,}\d")
PATH = re.compile(r"/Users/[^\s/]+")


class ReportRequest(BaseModel):
    kind: str
    title: str = ""
    text: str
    diagnostics: str = ""


def redact(text: str) -> str:
    """Nothing that points at a person: emails, phone-like numbers, a home
    folder's user name, and links reduced to their host."""
    text = EMAIL.sub("<email>", text)
    text = URL.sub(lambda m: f"<link {urlparse(m.group(0)).hostname or ''}>", text)
    text = PATH.sub("/Users/<me>", text)
    return LONG_NUMBER.sub("<number>", text)


def _run(*args: str) -> str:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=5,
                              cwd=paths.ROOT).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def diagnostics() -> str:
    from . import prompts as setup
    status = setup.setup_status()
    jobs = queue.all_jobs()
    counts = Counter(str(getattr(job.status, "value", job.status)) for job in jobs)
    lines = [
        f"version: {_run('git', 'describe', '--tags', '--match', 'v*', '--always') or 'unknown'}"
        f" (branch {_run('git', 'rev-parse', '--abbrev-ref', 'HEAD') or '?'}"
        f"{', local changes logged' if (paths.ROOT / 'LOCAL_CHANGES.md').exists() else ''})",
        f"macOS {platform.mac_ver()[0] or '?'} {platform.machine()}, Python {platform.python_version()}",
        "setup: " + ", ".join(f"{name} {'yes' if status.get(name) else 'no'}"
                              for name in ("key", "resume", "jobright", "chrome", "onboarded")),
        "jobs: " + (", ".join(f"{n} {state}" for state, n in sorted(counts.items())) or "none"),
        "edited prompts: " + (", ".join(p["name"] for p in prompts.listing() if p["edited"]) or "none"),
        "switches: " + ", ".join(f"{name}={os.environ[name]}" for name in SWITCHES if os.environ.get(name)),
    ]
    failed = [job for job in jobs if job.error][-5:]
    if failed:
        lines.append("recent errors:")
        for job in failed:
            host = urlparse(job.url or "").hostname or "?"
            first = (job.error or "").strip().splitlines()[0][:240]
            lines.append(f"- [{host}] {redact(first)}")
    return "\n".join(lines)


def issue_url(kind: str, title: str, text: str, diagnostics_text: str) -> str:
    template = TEMPLATES.get(kind)
    if template is None:
        raise HTTPException(400, "kind is problem or feature")
    prefix = "Problem: " if kind == "problem" else "Feature: "
    head = (f"{REPO}/issues/new?template={template}"
            f"&title={quote(prefix + title.strip())}&what={quote(text.strip())}")
    room = MAX_URL - len(head) - len("&diagnostics=")
    block = diagnostics_text.strip()
    while block and len(quote(block)) > room:
        block = block[: int(len(block) * 0.9)]
    return head + (f"&diagnostics={quote(block)}" if block else "")


@router.get("/report")
def get_report() -> dict:
    return {"diagnostics": diagnostics(), "repo": REPO}


@router.post("/report/open")
def open_report(body: ReportRequest) -> dict:
    if not body.text.strip():
        raise HTTPException(400, "say what happened, or what you would like")
    # Redacted again: the block was editable on the page.
    url = issue_url(body.kind, body.title, body.text, redact(body.diagnostics))
    # The default browser, where the person is signed in to GitHub; the
    # Autopilot Chrome usually is not. The page opens the link itself when
    # this cannot.
    try:
        opened = subprocess.run(["open", url], timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        opened = False
    return {"url": url, "opened": opened}
