"""Press Apply on a posting, so the application the person approved can start.

Workday, Oracle, iCIMS and most of the big employers keep the application
behind an Apply button and an account. The fill never pressed that button, on
the principle that pressing Apply starts an application on someone else's
terms - so a Workday job opened its tab, found a posting rather than a form,
waited the full ten minutes of `signin.SIGNIN_TIMEOUT` for a sign-in that
could not appear until Apply was pressed, and handed the job back. Ten minutes
of the one-tab-at-a-time queue, spent on a page where nothing was ever going
to happen (Globus Medical, 2026-09-30, job `3dcc`).

The principle it was protecting is intact, because Apply is not Submit:

- **Nothing is pressed unless the page has nothing to submit.** `safe_to_press`
  wants no file input and fewer than `signin.FORM_FIELDS` editable fields:
  a posting, in other words. A page holding a filled-in application is refused
  outright, whatever its buttons say.
- **The label has to read as the start of an application** (`APPLY_START`,
  anchored whole-string), and anything that reads as finishing one - submit,
  send, finish, withdraw - disqualifies it (`NOT_APPLY`), as does a cancel,
  a search, or a sign-in.
- The job was approved by the person at checkpoint 1, which is what this
  carries out. Checkpoint 2, the filled form, is untouched: this module has no
  path to a Submit control. It does not import `guard` and does not use the
  deny-list the agent is refused; the one module that presses a submit control
  is reachable only from the review page's own button, and this file may not
  even name it (`tests/test_invariants.py` #6 reads it as plain text).
- At most `MAX_PRESSES` presses per run, because Workday's Apply leads to a
  chooser ("Autofill with Resume" / "Apply Manually" / "Use My Last
  Application") and that chooser leads to the account. Three steps is the
  whole path; a fourth is a loop.

The press is a real `Input.dispatchMouseEvent` at the control's centre, not
`el.click()`, for the same reason the submit press is: React ignores a
scripted click on its primary control.
"""

from __future__ import annotations

import asyncio
import json as _json
import re
from typing import Optional

# How many Apply-ish controls one run may press. Posting -> chooser ->
# "Apply Manually" is the longest honest path.
MAX_PRESSES = 3
SETTLE = 1.0

# The start of an application, as the big systems word it. Anchored: a
# sentence in a description that happens to contain "apply now" is prose,
# and this has to be the whole label of a control.
APPLY_START = re.compile(
    r"^\s*(apply|apply now|apply online|apply for this job|apply to this job|"
    r"apply manually|apply with (your )?resume|autofill with resume|"
    r"use my last application|start (your )?application|begin application|"
    r"continue to application)\s*[.>»→]*\s*$", re.I)

# Anything that reads as finishing an application, leaving one, or going
# somewhere else entirely. Checked against the whole label, so a control
# called "Submit application" can never be the one this presses.
NOT_APPLY = re.compile(
    r"\b(submit|send|finish|complete|confirm|withdraw|delete|cancel|back|"
    r"previous|save|sign\s*in|log\s*in|search|filter|share|save\s*job|"
    r"email|print)\b", re.I)

_APPLY_SOURCE = _json.dumps(APPLY_START.pattern)

# Every clickable thing, in the page and in its shadow roots (Workday's
# controls live in custom elements), with the text a reader would call it by.
# The choice between them is made in Python, where it can be read and tested.
CANDIDATES_JS = r"""
(function () {
  const START = new RegExp(APPLY_PATTERN, "i");
  const out = [];
  const seen = new Set();
  const walk = (root) => {
    let nodes;
    try { nodes = root.querySelectorAll("button, input, a, [role=button]"); }
    catch (e) { return; }
    for (const el of nodes) {
      if (seen.has(el)) continue;
      seen.add(el);
      const tag = el.tagName.toLowerCase();
      const type = (el.getAttribute("type") || "").toLowerCase();
      if (tag === "input" && !["submit", "button", "image"].includes(type)) continue;
      const text = (el.innerText || el.value || el.getAttribute("aria-label")
                    || el.getAttribute("title") || "").trim().slice(0, 120);
      if (!text || !START.test(text)) continue;
      const rect = el.getBoundingClientRect();
      const style = getComputedStyle(el);
      out.push({
        text,
        tag,
        disabled: !!(el.disabled || el.getAttribute("aria-disabled") === "true"),
        shown: rect.width > 0 && rect.height > 0 && style.visibility !== "hidden"
               && style.display !== "none" && Number(style.opacity || "1") > 0.05,
        x: rect.left + rect.width / 2,
        y: rect.top + rect.height / 2,
        top: rect.top,
      });
    }
    for (const el of (root.querySelectorAll ? root.querySelectorAll("*") : [])) {
      if (el.shadowRoot) walk(el.shadowRoot);
    }
  };
  walk(document);
  return out;
})()
""".replace("APPLY_PATTERN", _APPLY_SOURCE)

SCROLL_JS = r"""
(function (x, y) {
  const el = document.elementFromPoint(x, y);
  if (el) el.scrollIntoView({ block: "center", behavior: "instant" });
  return true;
})
"""


def safe_to_press(look) -> bool:
    """Whether this page is a posting, with nothing on it that could be sent.

    Takes a `signin.Look`. A page with somewhere to attach a resume, or with
    a form's worth of editable fields, is an application in progress and is
    never pressed here, however its buttons are worded.
    """
    from .signin import FORM_FIELDS

    return not look.files and look.fields < FORM_FIELDS and not look.passwords


def choose(candidates: list[dict]) -> Optional[dict]:
    """The Apply control to press, or None.

    Shown and enabled, its whole label the start of an application and
    nothing about finishing one. The highest on the page wins: a posting's
    own Apply is above the footer's "apply online" boilerplate, and Workday's
    chooser lists its options top-down in the order it wants them taken.
    """
    live = [c for c in candidates
            if isinstance(c, dict) and c.get("shown") and not c.get("disabled")
            and APPLY_START.match(str(c.get("text") or ""))
            and not NOT_APPLY.search(str(c.get("text") or ""))]
    if not live:
        return None
    return sorted(live, key=lambda c: c.get("top") or 0)[0]


async def press(page, look) -> Optional[str]:
    """Press this page's Apply control. Returns its label, or None.

    `page` is a `signin.Session`-shaped page with `evaluate` and `send`.
    Nothing is pressed when the page is not a posting (`safe_to_press`) or
    when no control's whole label reads as the start of an application.
    """
    if not safe_to_press(look):
        return None
    candidates = await page.evaluate(CANDIDATES_JS) or []
    choice = choose([c for c in candidates if isinstance(c, dict)])
    if not choice:
        return None
    # The rectangle read before a scroll is where the element was, so scroll
    # first and read the point again.
    await page.evaluate(f"({SCROLL_JS})({choice['x']!r}, {choice['y']!r})")
    await asyncio.sleep(SETTLE / 2)
    fresh = await page.evaluate(CANDIDATES_JS) or []
    again = choose([c for c in fresh if isinstance(c, dict)])
    point = again if again and again.get("text") == choice.get("text") else choice
    for kind in ("mousePressed", "mouseReleased"):
        await page.send("Input.dispatchMouseEvent", {
            "type": kind, "x": point["x"], "y": point["y"],
            "button": "left", "clickCount": 1,
        })
    return str(point.get("text") or "").strip()
