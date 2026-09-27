"""The one place a Submit control is ever pressed, and the agent cannot reach it.

Everything else in `browser/` is built so that no automated step can submit an
application: `guard.describes_submit` refuses the click, `evaluate` and
`send_keys` are gone from the agent's vocabulary, and the fill stops on the
completed form with the window open. That rule is about the *agent*, not about
the person: the whole pipeline exists to put a finished application in front of
a human and let them decide.

This module is that decision carried out. It is called from one endpoint,
`POST /review/{id}/submit`, which is reachable only from the review page's
Submit button, behind a confirmation. Nothing under `browser/` imports it,
`apply.py` does not import it, and no browser-use action is registered for it;
`tests/test_invariants.py` checks all three, so the agent's reach does not grow
into it by accident later.

The same `guard.SUBMIT_PATTERNS` that tells the agent what it may never click
is what finds the control here. One list, two uses: refused for the agent,
aimed at for the person.

The press is a real mouse press at the control's centre, not `el.click()`:
React forms (Greenhouse, Ashby, Lever) ignore a scripted click on their submit
control the same way react-select ignores a scripted value.
"""

from __future__ import annotations

import asyncio
import json as _json
import re
import time
import urllib.request
from itertools import count
from typing import Optional

from . import guard
from .autofill import Session, _send

# How long to wait, after the press, for the page to become a confirmation.
# A form that is still on the screen after this is not a failure: validation
# may be complaining, or the site may be slow. The caller says so and leaves
# the job where it was; `server/watch.py` marks it if the confirmation lands
# later.
CONFIRM_TIMEOUT = 25.0
POLL = 0.5

# Anything whose text reads as a *cancel* or a *save for later* is not the
# control we want, however a site words its primary button.
NOT_SUBMIT = re.compile(r"\b(cancel|back|previous|save\s*(draft|for\s*later)?|"
                        r"delete|withdraw|sign\s*in|log\s*in|search)\b", re.I)

_SUBMIT_SOURCE = "[" + ", ".join(_json.dumps(p) for p in guard.SUBMIT_PATTERNS) + "]"

# Every clickable thing on the page and in its shadow roots, with the text a
# reader would call it by, whether it is inside a form, whether it is
# enabled and on the screen, and its centre. The choice between them is made
# in Python, where it can be read and tested.
CANDIDATES_JS = r"""
(function () {
  const PATTERNS = SUBMIT_PATTERNS.map((p) => new RegExp(p, "i"));
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
      if (!text) continue;
      if (!PATTERNS.some((re) => re.test(text))) continue;
      const rect = el.getBoundingClientRect();
      const style = getComputedStyle(el);
      const shown = rect.width > 0 && rect.height > 0
        && style.visibility !== "hidden" && style.display !== "none"
        && Number(style.opacity || "1") > 0.05;
      out.push({
        text,
        tag,
        type,
        disabled: !!(el.disabled || el.getAttribute("aria-disabled") === "true"),
        shown,
        in_form: !!el.closest("form"),
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
""".replace("SUBMIT_PATTERNS", _SUBMIT_SOURCE)

SCROLL_JS = r"""
(function (x, y) {
  const el = document.elementFromPoint(x, y);
  if (el) el.scrollIntoView({ block: "center", behavior: "instant" });
  return true;
})
"""

# The visible text and the number of form controls left, to tell a
# confirmation from a form that is still sitting there.
STATE_JS = r"""
(function () {
  const controls = document.querySelectorAll(
    "input:not([type=hidden]):not([type=submit]):not([type=button]), textarea, select").length;
  return { controls, text: (document.body ? document.body.innerText : "").slice(0, 4000) };
})()
"""


class NoSubmitControl(RuntimeError):
    """No control on the page reads as the form's submit button."""


def choose(candidates: list[dict]) -> Optional[dict]:
    """The one control to press, or None.

    Pressable and on the screen first; then a cancel-ish word disqualifies
    whatever else its label says; then `input[type=submit]` and a control
    inside a `<form>` are preferred, and the lowest on the page wins a tie,
    since a multi-step form puts its final action last.
    """
    live = [c for c in candidates
            if c.get("shown") and not c.get("disabled") and not NOT_SUBMIT.search(c.get("text", ""))]
    if not live:
        return None
    def rank(c: dict) -> tuple:
        return (c.get("type") == "submit", bool(c.get("in_form")), c.get("top") or 0)
    return sorted(live, key=rank)[-1]


def confirmed(state: dict) -> bool:
    """Whether the page the press led to is a confirmation: the form is gone
    and the text says so. Both halves, because "thank you for your interest"
    is in half the postings and a form that failed validation keeps its
    fields."""
    from server import corrections
    from server.watch import CONFIRMED

    if int(state.get("controls") or 0) >= corrections.MIN_FIELDS:
        return False
    return bool(CONFIRMED.search(str(state.get("text") or "")))


async def press(cdp_url: str, target_id: str) -> dict:
    """Press the form's submit control in that tab and watch what happens.

    Returns `{"pressed": <label>, "confirmed": bool, "text": <the
    confirmation's own words, when there are any>}`. Raises
    `NoSubmitControl` when there is nothing to press, in which case nothing
    was touched. The tab is left open either way: the person is looking at
    it, and a confirmation is what they came for.
    """
    from websockets.asyncio.client import connect

    with urllib.request.urlopen(f"{cdp_url}/json/version", timeout=5) as response:
        ws_url = _json.loads(response.read())["webSocketDebuggerUrl"]
    async with connect(ws_url, max_size=None) as socket:
        ids = count(1)
        attached = await _send(socket, "Target.attachToTarget",
                               {"targetId": target_id, "flatten": True}, None, ids)
        page = Session(socket, attached["sessionId"], ids)
        await page.send("Runtime.enable")
        try:
            candidates = await page.evaluate(CANDIDATES_JS) or []
            choice = choose([c for c in candidates if isinstance(c, dict)])
            if not choice:
                raise NoSubmitControl(
                    "no submit button found on the form; submit it in the browser")
            # The rectangle read before a scroll is where the element was, so
            # scroll first and read the point again.
            await page.evaluate(
                f"({SCROLL_JS})({choice['x']!r}, {choice['y']!r})")
            await asyncio.sleep(POLL)
            fresh = await page.evaluate(CANDIDATES_JS) or []
            again = choose([c for c in fresh if isinstance(c, dict)])
            point = again if again and again.get("text") == choice.get("text") else choice
            for kind in ("mousePressed", "mouseReleased"):
                await page.send("Input.dispatchMouseEvent", {
                    "type": kind, "x": point["x"], "y": point["y"],
                    "button": "left", "clickCount": 1,
                })

            deadline = time.monotonic() + CONFIRM_TIMEOUT
            state: dict = {}
            while time.monotonic() < deadline:
                await asyncio.sleep(POLL)
                try:
                    state = await page.evaluate(STATE_JS) or {}
                except Exception:  # noqa: BLE001 - mid-navigation, look again
                    continue
                if confirmed(state):
                    from server.watch import CONFIRMED
                    match = CONFIRMED.search(str(state.get("text") or ""))
                    return {"pressed": choice["text"], "confirmed": True,
                            "text": match.group(0) if match else ""}
            return {"pressed": choice["text"], "confirmed": False, "text": ""}
        finally:
            try:
                await _send(socket, "Target.detachFromTarget",
                            {"sessionId": page.session_id}, None, ids)
            except Exception:  # noqa: BLE001
                pass


def press_now(cdp_url: str, target_id: str) -> dict:
    """`press` from ordinary (non-async) server code."""
    return asyncio.run(press(cdp_url, target_id))
