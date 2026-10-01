"""LinkedIn Easy Apply, page by page, stopping at the review.

Easy Apply is five or six short pages inside the job page itself — contact
details, the resume, screening questions, demographics, then a review with
**Submit application** on it. The same shape as the Workday walk, with three
differences that come from LinkedIn rather than from us:

1. **Nothing may be selected by class.** The page's class names are generated
   and rotate (`bghmnt bghmns bghr4 bgha1f …` on the apply button itself,
   measured 2026-09-30). Everything here hangs off what a reader sees: the
   `aria-label`, the visible text, and the `N/M pages` the flow prints at the
   top of every step.
2. **The flow is not a `[role=dialog]`.** It replaces the page's own content,
   so "are we in the flow?" is answered by that `N/M pages` line and the
   `Apply to <company>` heading, not by a modal selector.
3. **The questions are the person's.** The decision (2026-09-30) is that the
   tailored resume goes on every time and nothing else is answered: LinkedIn's
   screening questions are where a wrong answer is expensive, and half of
   them are the work-authorisation questions this app may never touch
   (`guard.check_protected`). So the walk fills the resume, advances while
   there is nothing to answer, and hands the tab over the moment a page wants
   something — which is usually page three.

It never presses **Submit application**: the review page is the end of the
line here exactly as the filled form is everywhere else, and the person's own
click is what follows. `guard.describes_submit` is checked against every
control before it is pressed, so a page that words its Next button badly
stops the walk rather than sending the application.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Optional

from . import guard
from .autofill import Session

log = logging.getLogger(__name__)

HOSTS = ("linkedin.com",)
MAX_PAGES = 12          # Easy Apply is five or six; twelve is a loop
SETTLE = 1.2
ADVANCE_TIMEOUT = 20.0

# "1/5 pages", which every step of the flow prints.
PROGRESS = re.compile(r"\b(\d+)\s*/\s*(\d+)\s*pages?\b", re.I)
# The heading the flow opens with.
APPLYING = re.compile(r"^\s*Apply to\b", re.I | re.M)

# The control that moves on. "Review" is the last of them; "Submit
# application" is not here and never will be.
NEXT = re.compile(r"^\s*(continue to next step|next|review your application|review)\s*$", re.I)
UPLOAD = re.compile(r"^\s*(upload resume|upload a resume|choose file|upload)\s*$", re.I)
DELETE_UPLOAD = re.compile(r"^\s*(delete|remove|remove file|delete file)\b", re.I)
# What a page says when it will not move on.
REQUIRED = re.compile(r"please enter|required|must be|enter a (valid|whole) number|"
                      r"select an option|answer this question", re.I)


def is_linkedin(url: str) -> bool:
    host = (url or "").split("://")[-1].split("/")[0].lower()
    return any(host == h or host.endswith("." + h) for h in HOSTS)


# One look at the flow: where it is, what it is asking, what can be pressed.
# Everything is read from text and aria, because the class names rotate.
PAGE_JS = r"""
(() => {
  const vis = (el) => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== "hidden" && s.display !== "none";
  };
  const label = (el) => (el.getAttribute("aria-label") || el.innerText || "").replace(/\s+/g, " ").trim();
  const buttons = [];
  for (const el of document.querySelectorAll("button, [role=button]")) {
    if (!vis(el)) continue;
    const text = label(el).slice(0, 80);
    if (!text) continue;
    const r = el.getBoundingClientRect();
    buttons.push({ text, disabled: !!(el.disabled || el.getAttribute("aria-disabled") === "true"),
                   x: r.left + r.width / 2, y: r.top + r.height / 2, top: r.top });
  }
  // Every control the person could still have to answer, with the question
  // a reader would call it by. A radio group counts once.
  const asked = [];
  const seen = new Set();
  let editable = 0, files = 0, empty = 0;
  for (const el of document.querySelectorAll("input, select, textarea")) {
    const type = (el.type || "").toLowerCase();
    if (type === "hidden" || type === "submit" || type === "button") continue;
    if (type === "file") { files++; continue; }
    if (!vis(el) && type !== "radio") continue;
    editable++;
    const group = el.closest("fieldset, [role=group], [data-test-form-element]") || el.parentElement;
    const heading = group && group.querySelector("label, legend, span[data-test-form-element-label]");
    const q = (heading ? heading.innerText : (el.getAttribute("aria-label") || "")).replace(/\s+/g, " ").trim();
    const answered = type === "radio" || type === "checkbox"
      ? !!(group && group.querySelector("input:checked"))
      : !!(el.value || "").trim();
    if (!answered) empty++;
    if (q && !seen.has(q)) { seen.add(q); asked.push({ q: q.slice(0, 160), answered, type }); }
  }
  // The resume card: what is on the slot now, so a second one never goes on.
  const body = document.body ? document.body.innerText : "";
  return {
    url: location.href,
    text: body.slice(0, 4000),
    heads: [...document.querySelectorAll("h1, h2, h3")].filter(vis)
             .map((h) => (h.innerText || "").trim().slice(0, 90)).filter(Boolean).slice(0, 6),
    buttons: buttons.slice(0, 60),
    editable, files, empty,
    asked: asked.slice(0, 25),
  };
})()
"""


@dataclass
class Step:
    number: int
    stage: str
    heading: str = ""
    page: str = ""              # "2/5" as the flow prints it
    resume_uploaded: bool = False
    advanced: bool = False
    asks: list = field(default_factory=list)
    errors: list = field(default_factory=list)


@dataclass
class EasyApplyResult:
    target_id: str = ""
    steps: list = field(default_factory=list)
    stage: str = ""
    reached_review: bool = False
    paused: str = ""
    detail: str = ""
    asks: list = field(default_factory=list)   # what it stopped on, by question
    resume_uploaded: bool = False
    errors: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.reached_review and self.resume_uploaded

    def summary(self) -> str:
        parts = [f"{len(self.steps)} page(s)"]
        parts.append("resume on" if self.resume_uploaded else "resume NOT on")
        if self.reached_review:
            parts.append("reached the review page")
        if self.paused:
            parts.append(f"paused: {self.paused}")
        return "; ".join(parts)

    def to_json(self) -> dict:
        return {"target_id": self.target_id, "stage": self.stage,
                "reached_review": self.reached_review, "paused": self.paused,
                "detail": self.detail, "asks": self.asks,
                "resume_uploaded": self.resume_uploaded, "errors": self.errors,
                "steps": [vars(s) for s in self.steps]}


PAUSES = {
    "not_in_the_flow": "the Easy Apply flow is not open: press Easy Apply in the browser, "
                       "then press Fill the form again",
    "questions": "LinkedIn is asking questions only you can answer; answer them in the "
                 "browser and press Next until the review page",
    "stuck": "the page did not move on and said nothing about why; look at the browser",
    "too_many_pages": "the flow went round more pages than one ever should; look at the browser",
}


def progress(look: dict) -> str:
    found = PROGRESS.search(str(look.get("text") or ""))
    return f"{found.group(1)}/{found.group(2)}" if found else ""


def in_flow(look: dict) -> bool:
    """Whether the Easy Apply flow is on the screen."""
    text = str(look.get("text") or "")
    return bool(PROGRESS.search(text) or APPLYING.search(text))


def stage_of(look: dict) -> str:
    """`review`, `resume`, `questions`, `form`, or `away`."""
    if not in_flow(look):
        return "away"
    heads = " | ".join(look.get("heads") or [])
    text = str(look.get("text") or "")
    buttons = [str(b.get("text") or "") for b in look.get("buttons") or []]
    if any(guard.describes_submit(b) for b in buttons):
        return "review"
    if re.search(r"review your application|^review$", heads, re.I):
        return "review"
    if int(look.get("files") or 0) or re.search(r"\bresume\b", heads, re.I) or \
            any(UPLOAD.match(b) for b in buttons):
        return "resume"
    if int(look.get("empty") or 0):
        return "questions"
    return "form"


def next_button(look: dict) -> Optional[dict]:
    """The control that moves to the next page, or None.

    A submit is never a candidate, however the page words it: that check
    happens before anything is returned, not after.
    """
    live = [b for b in look.get("buttons") or []
            if NEXT.match(str(b.get("text") or "")) and not b.get("disabled")
            and not guard.describes_submit(str(b.get("text") or ""))]
    return sorted(live, key=lambda b: b.get("top") or 0)[-1] if live else None


def open_questions(look: dict) -> list[str]:
    """The questions this page is still waiting on, as a reader sees them."""
    return [str(a.get("q")) for a in look.get("asked") or [] if not a.get("answered")][:12]


# ---------------------------------------------------------------- the walk --

async def look_at(page: Session) -> dict:
    return (await page.evaluate(PAGE_JS)) or {}


def _shape(look: dict) -> tuple:
    return (progress(look), " | ".join(look.get("heads") or []),
            int(look.get("editable") or 0), int(look.get("files") or 0))


async def click(page: Session, button: dict) -> None:
    """Press a control at its centre with a real mouse event. LinkedIn
    ignores `el.click()` on its own controls, the same as every other React
    form this app touches; the caller has already refused anything that reads
    as a submit."""
    await page.evaluate(
        "((x, y) => { const el = document.elementFromPoint(x, y);"
        " if (el) el.scrollIntoView({block: 'center', behavior: 'instant'}); return true; })"
        f"({button['x']!r}, {button['y']!r})")
    await asyncio.sleep(SETTLE / 2)
    for kind in ("mousePressed", "mouseReleased"):
        await page.send("Input.dispatchMouseEvent", {
            "type": kind, "x": button["x"], "y": button["y"], "button": "left", "clickCount": 1})


async def advance(page: Session, before: tuple, timeout: Optional[float] = None) -> dict:
    """Wait for the flow to become a different page. The timeout is read here
    rather than frozen into the signature."""
    deadline = time.monotonic() + (ADVANCE_TIMEOUT if timeout is None else timeout)
    look: dict = {}
    while time.monotonic() < deadline:
        await asyncio.sleep(SETTLE)
        try:
            look = await look_at(page)
        except Exception:  # noqa: BLE001 - mid-render, look again
            continue
        if look and _shape(look) != before:
            return look
    return look


async def put_resume(page: Session, resume) -> bool:
    """Put the tailored resume on this page's slot, clearing whatever is on it.

    The decision (2026-09-30) is that it goes on every time rather than
    reusing whatever LinkedIn has stored: the stored one is the master, and
    the whole point of the pipeline is that this job gets its own. An
    occupied slot is emptied first, for the same reason it is on Workday -
    two resumes on a form and nobody can tell which was sent.
    """
    from .forms.engine import FILE_NAME_FN, FIND_FN

    sent = await page.evaluate(r"""
    (() => {
      const inputs = [...document.querySelectorAll('input[type=file]')];
      if (!inputs.length) return null;
      const input = inputs[0];
      input.setAttribute('data-autopilot-ref', 'li-resume');
      return {name: (input.files && input.files[0] && input.files[0].name) || ''};
    })()
    """)
    if sent is None:
        return False
    handle = await page.send("Runtime.evaluate", {
        "expression": f"({FIND_FN.strip()})(document, \"li-resume\")"})
    object_id = (handle.get("result") or {}).get("objectId")
    if not object_id:
        return False
    node = await page.send("DOM.describeNode", {"objectId": object_id})
    backend = (node.get("node") or {}).get("backendNodeId")
    await page.send("DOM.setFileInputFiles",
                    {"files": [str(resume.resolve())], "backendNodeId": backend})
    await asyncio.sleep(SETTLE * 2)
    name = await page.evaluate(
        f"({FILE_NAME_FN.strip()})(\"li-resume\", {resume.name!r})")
    return str(name or "") == resume.name


async def walk(cdp_url: str, target_id: str, resume=None,
               max_pages: int = MAX_PAGES) -> EasyApplyResult:
    """Walk an open Easy Apply flow as far as the review page.

    The flow has to be open already: pressing **Easy Apply** is
    `browser/open_apply.py`'s job and happens before this, and the account and
    the sign-in are the person's. Nothing here answers a question, and
    nothing here presses Submit.
    """
    from .autofill import attached

    result = EasyApplyResult(target_id=target_id)
    async with attached(cdp_url, target_id=target_id) as (page, _):
        await page.send("DOM.enable")
        for number in range(1, max_pages + 1):
            try:
                look = await look_at(page)
            except Exception as error:  # noqa: BLE001
                result.errors.append(f"reading page {number}: {error}")
                break
            stage = stage_of(look)
            result.stage = stage
            step = Step(number=number, stage=stage, page=progress(look),
                        heading=(look.get("heads") or [""])[0])
            result.steps.append(step)

            if stage == "away":
                result.paused, result.detail = "not_in_the_flow", PAUSES["not_in_the_flow"]
                break
            if stage == "review":
                # The end of the line. Submit is the person's.
                result.reached_review = True
                break

            if stage == "resume" and resume is not None and not result.resume_uploaded:
                try:
                    step.resume_uploaded = await put_resume(page, resume)
                    result.resume_uploaded = result.resume_uploaded or step.resume_uploaded
                except Exception as error:  # noqa: BLE001 - the person can attach it
                    step.errors.append(f"resume: {error}")
                    result.errors.append(step.errors[-1])

            asks = open_questions(look)
            step.asks = asks
            if asks:
                # LinkedIn's screening questions are the person's: half of them
                # are work-authorisation questions this app may never answer,
                # and a wrong answer on the rest is expensive.
                result.asks = asks
                result.paused, result.detail = "questions", PAUSES["questions"]
                break

            before = _shape(look)
            button = next_button(look)
            if button is None:
                result.paused = "stuck"
                result.detail = PAUSES["stuck"]
                break
            await click(page, button)
            after = await advance(page, before)
            step.advanced = bool(after) and _shape(after) != before
            if not step.advanced:
                result.asks = open_questions(after or look)
                result.paused = "questions" if result.asks else "stuck"
                result.detail = PAUSES[result.paused]
                break
        else:
            result.paused, result.detail = "too_many_pages", PAUSES["too_many_pages"]
    return result
