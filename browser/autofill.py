"""Press Jobright's Autofill button without a model.

The first thing the browser agent did on every form was find the Autofill
button, click it, and wait, which is the same three actions each time and
costs a few model steps per run. This does it in code over raw CDP, on our
own Chrome (port 9333), before the agent starts: open the form in a new
tab, wait for it to render, find the one control whose own text is
"Autofill", click it, and wait for the form to fill. The agent then takes
over in that tab with step one already done.

Anything unexpected — no button, the click changed nothing, the page never
loaded — ends here with `clicked=False` and the agent does what it always
did. Nothing is ever closed, and the finder cannot match a submit control:
the text must start with "autofill" and the deny-list words are refused
before the click (`guard.describes_submit`).

The one deterministic flow so far; the harness that grows more of them
from what the agent does is later work.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from itertools import count
from typing import Optional

from . import guard

log = logging.getLogger("autofill")

LOAD_TIMEOUT = 30.0
BUTTON_TIMEOUT = 20.0   # Jobright's extension injects the panel after load
FINISH_TIMEOUT = 60.0
POLL = 1.0
SETTLE_POLLS = 3        # filled-field count unchanged this many polls = done
MAX_PRESSES = 2         # the panel opener, then the control inside it

# Finds the Autofill control, in the page and in every open shadow root.
# Returns the text of what it found, or "" for nothing. Does not click.
FIND_JS = r"""
(() => {
  const AUTOFILL = /^\s*(auto-?fill)\b/i;
  const out = [];
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== "hidden" && s.display !== "none";
  };
  const walk = (root) => {
    for (const el of root.querySelectorAll("button, a, [role=button], div, span")) {
      // textContent first: innerText forces layout, and most elements are not it.
      const raw = (el.textContent || "").trim();
      const label = (el.getAttribute("aria-label") || el.getAttribute("title") || "").trim();
      if (!(raw.length < 60 && AUTOFILL.test(raw)) && !AUTOFILL.test(label)) continue;
      const own = (el.innerText || raw).trim();
      const text = AUTOFILL.test(own) && own.length < 40 ? own : AUTOFILL.test(label) ? label : "";
      if (!text || !visible(el)) continue;
      // The innermost match: a wrapper around the button matches too.
      if ([...el.children].some((c) => AUTOFILL.test((c.textContent || "").trim()))) continue;
      out.push({ el, text, tag: el.tagName.toLowerCase() });
    }
    // Shadow roots hang off any element, not only clickable ones; Jobright's
    // panel is a custom element.
    for (const host of root.querySelectorAll("*")) if (host.shadowRoot) walk(host.shadowRoot);
  };
  walk(document);
  if (!out.length) return null;
  // "Autofill" over "Autofill for another job": the shortest text wins.
  out.sort((a, b) => a.text.length - b.text.length);
  for (const c of out) c.el.removeAttribute("data-autopilot-autofill");
  out[0].el.setAttribute("data-autopilot-autofill", "1");
  return { text: out[0].text, tag: out[0].tag };
})()
"""

CLICK_JS = r"""
(() => {
  const find = (root) => {
    const el = root.querySelector("[data-autopilot-autofill]");
    if (el) return el;
    for (const host of root.querySelectorAll("*")) {
      if (host.shadowRoot) { const inner = find(host.shadowRoot); if (inner) return inner; }
    }
    return null;
  };
  const el = find(document);
  if (!el) return false;
  el.click();
  return true;
})()
"""

# Jobright reports its progress with window.top.postMessage(..., "*"):
# `updateResultFromIframe` snapshots while it works (filledFields,
# missingFields, currentField), `autoFillResultFromIframe` with the final
# lists, `autoFillCompleteFromIframe` when it stops without them. Read out
# of their extension's bundle; the DOM event it also fires only goes out on
# jobright.ai. Installed before the click, buffered on window.
LISTEN_JS = r"""
(() => {
  if (window.__autopilotJR) return true;
  const state = { events: 0, done: false, current: null, filled: [], missing: [] };
  window.__autopilotJR = state;
  window.addEventListener("message", (event) => {
    const type = event.data && event.data.type;
    if (!type || !/^(updateResultFromIframe|autoFillResultFromIframe|autoFillCompleteFromIframe)$/.test(type)) return;
    state.events++;
    const data = event.data.data || {};
    const inner = data.data || data;
    if (Array.isArray(inner.filledFields)) state.filled = inner.filledFields;
    if (Array.isArray(inner.missingFields)) state.missing = inner.missingFields;
    if ("currentField" in inner) state.current = inner.currentField;
    if (type !== "updateResultFromIframe") state.done = true;
  }, true);
  return true;
})()
"""

# How far along the fill is: filled field count, what Jobright's panel
# says, and what Jobright's own messages say. The panel is the deterministic
# reading: it lives in the shadow root of `plasmo-csui#jobright-helper-plugin`
# (innerText of the page never sees it) and shows "Autofilling" with three
# dots while their fill runs, then "N/M required fields filled | X%" once it
# stops. Read from their bundle: `loading ? "Autofilling" : ctaText` on the
# button, `progressTitle: "Autofilling"` on the dashboard while `isFilling`,
# `filledFields.length "/" totalFields.length "required fields filled"` after.
STATUS_JS = r"""
(() => {
  let filled = 0;
  const walk = (root) => {
    for (const el of root.querySelectorAll("input, textarea, select")) {
      if (el.type === "hidden" || el.type === "submit" || el.type === "button") continue;
      if (el.type === "checkbox" || el.type === "radio") { if (el.checked) filled++; continue; }
      if ((el.value || "").trim()) filled++;
    }
    for (const host of root.querySelectorAll("*")) if (host.shadowRoot) walk(host.shadowRoot);
  };
  walk(document);
  // Text of every shadow root: Jobright's panel is a custom element and the
  // page's innerText stops at its boundary.
  const shadowText = (root) => {
    let text = "";
    for (const host of root.querySelectorAll("*")) {
      if (!host.shadowRoot) continue;
      for (const child of host.shadowRoot.children) if (child.tagName !== "STYLE") text += "\n" + (child.innerText || "");
      text += shadowText(host.shadowRoot);
    }
    return text;
  };
  const text = (document.body ? document.body.innerText : "") + shadowText(document);
  const finished = /autofill(ed)?\s+(is\s+)?(complete|finished|done)|filled\s+\d+\s+fields|autofill\s+successful/i.test(text);
  const busy = /\bAutofilling\b/.test(text);
  const count = text.match(/(\d+)\s*\/\s*(\d+)\s+(?:required\s+)?fields\s+filled/i);
  const panel = { busy, done: !busy && !!count,
                  filled: count ? Number(count[1]) : null, total: count ? Number(count[2]) : null };
  const jr = window.__autopilotJR || null;
  return { filled, finished, panel, signal: jr && { events: jr.events, done: jr.done, current: jr.current,
           filled: jr.filled, missing: jr.missing } };
})()
"""


# Tells the page's banner what automation is doing to it, so the human has
# a signal other than a still cursor. content.js installs the function; a
# page without it (the script not injected yet) simply ignores the call.
def signal_js(state: str, note: str = "") -> str:
    return (f"window.__autopilotAutomation && window.__autopilotAutomation("
            f"{json.dumps(state)}, {json.dumps(note)})")


@dataclass
class AutofillResult:
    target_id: str = ""
    clicked: bool = False
    finished: bool = False
    filled_before: int = 0
    filled_after: int = 0
    note: str = ""
    missing: list = field(default_factory=list)   # labels Jobright reported empty
    signalled: bool = False                        # Jobright's own message ended the wait
    panel: str = ""                                # "9/10" as Jobright's panel showed it

    @property
    def tab_id(self) -> str:
        """The id browser-use's switch action wants: the last four characters."""
        return self.target_id[-4:]

    def summary(self) -> str:
        if not self.clicked:
            return f"autofill: not pressed by code ({self.note})"
        how = ("Jobright reported done" if self.signalled else "panel reported done" if self.finished
               else "fields stopped changing")
        text = f"autofill: pressed by code, {self.filled_before} → {self.filled_after} fields ({how})"
        if self.panel:
            text += f"; panel says {self.panel} filled"
        if self.missing:
            text += f"; Jobright left empty: {', '.join(self.missing[:12])}"
        return text


class Session:
    """One attached page over the browser's CDP socket."""

    def __init__(self, socket, session_id: str, ids=None):
        self.socket = socket
        self.session_id = session_id
        # Message ids are per connection, not per session; share the counter.
        self.ids = ids or count(1)

    async def send(self, method: str, params: Optional[dict] = None) -> dict:
        return await _send(self.socket, method, params, self.session_id, self.ids)

    async def evaluate(self, expression: str):
        result = await self.send("Runtime.evaluate", {
            "expression": expression, "returnByValue": True, "awaitPromise": True,
        })
        if "exceptionDetails" in result:
            raise RuntimeError(result["exceptionDetails"].get("text", "script threw"))
        return result.get("result", {}).get("value")


async def _send(socket, method: str, params: Optional[dict], session_id: Optional[str], ids) -> dict:
    message_id = next(ids)
    message: dict = {"id": message_id, "method": method, "params": params or {}}
    if session_id:
        message["sessionId"] = session_id
    await socket.send(json.dumps(message))
    # Events arrive interleaved; read until our reply comes.
    async for raw in socket:
        reply = json.loads(raw)
        if reply.get("id") == message_id:
            if "error" in reply:
                raise RuntimeError(f"{method}: {reply['error'].get('message')}")
            return reply.get("result", {})
    raise RuntimeError(f"{method}: socket closed")


async def wait_for_load(page: Session, timeout: float = LOAD_TIMEOUT) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if await page.evaluate("document.readyState") == "complete":
                await asyncio.sleep(1.5)  # client-side render after load
                return
        except RuntimeError:
            pass
        await asyncio.sleep(0.5)


async def run(page: Session, target_id: str = "") -> AutofillResult:
    """The flow on an attached page: find, check, click, wait. Split from
    `press` so it can run against a fake page in tests."""
    result = AutofillResult(target_id=target_id)
    await wait_for_load(page)

    found = None
    deadline = time.monotonic() + BUTTON_TIMEOUT
    while time.monotonic() < deadline:
        found = await page.evaluate(FIND_JS)
        if found:
            break
        await asyncio.sleep(POLL)
    if not found:
        result.note = "no Autofill button on the page"
        return result
    text = str(found.get("text", ""))
    if guard.describes_submit(text):
        # Cannot happen with the regex above, and is checked anyway: this
        # is the one click code makes on a form.
        result.note = f"refused: {text!r} reads as submit"
        return result

    try:
        await page.evaluate(LISTEN_JS)
    except RuntimeError:
        pass  # the wait then falls back to the field count
    # A native file chooser blocks the renderer, and with it every script
    # here, until a human dismisses it; one froze a tab for good. While the
    # press runs, a chooser is reported as an event instead of opened.
    intercepting = False
    try:
        await page.send("Page.setInterceptFileChooserDialog", {"enabled": True})
        intercepting = True
    except Exception:  # noqa: BLE001 - older Chrome; the press still goes ahead
        pass
    status = await page.evaluate(STATUS_JS) or {}
    result.filled_before = int(status.get("filled", 0))
    if not await page.evaluate(CLICK_JS):
        result.note = "the Autofill button vanished before the click"
        return result
    result.clicked = True
    log.info("pressed %r", text)

    # Wait for Jobright to say it is done: its own message, its panel
    # settling on "N/M fields filled" with no "Autofilling" in sight, or as a
    # last resort the field count growing and then holding still.
    last, stable = result.filled_before, 0
    idle_polls = 0            # polls in a row with the panel showing a count and not busy
    saw_busy = False
    pressed = [text]
    deadline = time.monotonic() + FINISH_TIMEOUT
    while time.monotonic() < deadline:
        await asyncio.sleep(POLL)
        status = await page.evaluate(STATUS_JS) or {}
        filled = int(status.get("filled", 0))
        signal = status.get("signal") or {}
        panel = status.get("panel") or {}
        if panel.get("busy"):
            saw_busy = True
            idle_polls = 0
        elif panel.get("done"):
            idle_polls += 1
        else:
            idle_polls = 0
        if panel.get("done"):
            result.panel = f"{panel.get('filled')}/{panel.get('total')}"
        # Jobright's panel has two steps on some pages: the first control
        # opens the panel and a differently worded one inside it starts the
        # fill. While nothing has happened, a new control gets one press.
        if (not signal.get("events") and not saw_busy and filled == result.filled_before
                and len(pressed) < MAX_PRESSES):
            again = await page.evaluate(FIND_JS)
            if again and again.get("text") not in pressed and not guard.describes_submit(str(again.get("text", ""))):
                if await page.evaluate(CLICK_JS):
                    pressed.append(str(again.get("text")))
                    idle_polls = 0
                    log.info("pressed %r", again.get("text"))
                    continue
        if signal.get("missing"):
            result.missing = [str(m) for m in signal["missing"]][:40]
        # Jobright's own word first: a final result, or a snapshot with no
        # field in progress after at least one with.
        if signal.get("done") or (signal.get("events", 0) > 1 and signal.get("current") is None
                                  and (signal.get("filled") or signal.get("missing"))):
            result.finished = result.signalled = True
            result.filled_after = filled
            break
        # The panel: "Autofilling" came and went, or a count has sat there
        # untouched long enough that no fill is starting.
        if panel.get("done") and (saw_busy or idle_polls >= SETTLE_POLLS):
            result.finished = True
            result.filled_after = filled
            break
        if status.get("finished"):
            result.finished = True
            result.filled_after = filled
            break
        stable = stable + 1 if filled == last else 0
        last = filled
        if filled > result.filled_before and stable >= SETTLE_POLLS and not panel.get("busy"):
            result.filled_after = filled
            break
    else:
        result.filled_after = last
        result.note = "autofill did not report finishing; fields " + (
            "grew" if last > result.filled_before else "did not change")
    if intercepting:
        try:
            await page.send("Page.setInterceptFileChooserDialog", {"enabled": False})
        except Exception:  # noqa: BLE001
            pass
    return result


@asynccontextmanager
async def attached(cdp_url: str, url: str = "", target_id: str = ""):
    """A tab on the browser at `cdp_url`, attached over CDP: a new one open
    on `url`, or the existing `target_id`. Yields (page, target_id) and
    detaches afterwards. The tab stays open; nothing here closes anything."""
    import urllib.request

    from websockets.asyncio.client import connect  # browser-use's dependency

    with urllib.request.urlopen(f"{cdp_url}/json/version", timeout=5) as response:
        ws_url = json.loads(response.read())["webSocketDebuggerUrl"]
    async with connect(ws_url, max_size=None) as socket:
        ids = count(1)
        if not target_id:
            created = await _send(socket, "Target.createTarget", {"url": url}, None, ids)
            target_id = created["targetId"]
        attached_ = await _send(socket, "Target.attachToTarget", {"targetId": target_id, "flatten": True}, None, ids)
        page = Session(socket, attached_["sessionId"], ids)
        await page.send("Page.enable")
        await page.send("Runtime.enable")
        try:
            yield page, target_id
        finally:
            try:
                await _send(socket, "Target.detachFromTarget", {"sessionId": page.session_id}, None, ids)
            except Exception:  # noqa: BLE001 - detaching is a courtesy
                pass


async def press(cdp_url: str, url: str) -> AutofillResult:
    """Open `url` in a new tab on the browser at `cdp_url`, press Autofill,
    wait, detach. The tab stays open for the agent."""
    async with attached(cdp_url, url) as (page, target_id):
        return await run(page, target_id)


# Query parameters that name the visitor, not the job. Everything else in
# the query is part of which form this is: Greenhouse's embedded board is
# one path for every job (`/embed/job_app?for=<company>&token=<id>`), and
# matching on the path alone put one job's resume on another job's form.
VOLATILE_PARAMS = ("jr_id", "gh_src", "source", "src", "ref", "fbclid", "gclid")


def same_page(a: str, b: str) -> bool:
    """Whether two URLs are the same form: host, path, and every query
    parameter that is not a visitor tag. Fragments are ignored."""
    from urllib.parse import parse_qsl, urlsplit

    def key(url: str):
        parts = urlsplit((url or "").strip())
        host = (parts.hostname or "").lower().removeprefix("www.")
        query = sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False)
                       if k.lower() not in VOLATILE_PARAMS and not k.lower().startswith("utm_"))
        return host, parts.path.rstrip("/") or "/", tuple(query)

    ka, kb = key(a), key(b)
    return bool(ka[0]) and ka == kb


def find_tab(cdp_url: str, url: str) -> str:
    """The open tab on `url` (see `same_page`), or ""."""
    import urllib.request

    try:
        with urllib.request.urlopen(f"{cdp_url}/json", timeout=5) as response:
            pages = [p for p in json.loads(response.read()) if p.get("type") == "page"]
    except Exception:  # noqa: BLE001 - no browser, no tab
        return ""
    for p in pages:
        if same_page(url, p.get("url") or ""):
            return p.get("id", "")
    return ""


def from_status(status: dict, target_id: str) -> Optional[AutofillResult]:
    """An AutofillResult from a tab's own record of a press already made
    there (the injector presses Autofill the moment a form opens, and its
    listener leaves Jobright's messages on the window; failing that, the
    panel itself shows "N/M fields filled" once their fill has run). None
    when the tab holds no such record, or the panel is still autofilling."""
    signal = (status or {}).get("signal") or {}
    panel = (status or {}).get("panel") or {}
    if panel.get("busy"):
        return None
    if not signal.get("events") and not panel.get("done"):
        return None
    result = AutofillResult(target_id=target_id, clicked=True, signalled=bool(signal.get("events")),
                            finished=bool(signal.get("done")) or signal.get("current") is None,
                            filled_after=int(status.get("filled", 0)),
                            missing=[str(m) for m in (signal.get("missing") or [])][:40],
                            panel=f"{panel.get('filled')}/{panel.get('total')}" if panel.get("done") else "",
                            note="pressed when the tab opened")
    return result


async def reuse(cdp_url: str, url: str) -> Optional[AutofillResult]:
    """The press already made in the tab the human opened, if there is one:
    the fill then works in that tab instead of opening a second one and
    pressing again."""
    target_id = find_tab(cdp_url, url)
    if not target_id:
        return None
    # Jobright may still be at work in that tab (the pipeline can be quicker
    # than its fill on a long form). Wait for it there rather than open a
    # second tab and press again beside it.
    deadline = time.monotonic() + FINISH_TIMEOUT
    async with attached(cdp_url, target_id=target_id) as (page, _):
        while True:
            status = await page.evaluate(STATUS_JS) or {}
            if not (status.get("panel") or {}).get("busy") or time.monotonic() >= deadline:
                break
            await asyncio.sleep(POLL)
        result = from_status(status, target_id)
        if result is None:
            # The tab is open but holds no press (no Autofill button on this
            # site, or the injector never saw the tab): press here, in this
            # tab, so the fill never has two tabs on one job.
            result = await run(page, target_id)
            if not result.clicked:
                result.note = f"in the open tab: {result.note}"
    return result


async def notify(cdp_url: str, target_id: str, state: str, note: str = "") -> None:
    """Show `state` on the tab's banner. Best effort: a tab that is gone, or
    has no banner, changes nothing about the fill."""
    try:
        async with attached(cdp_url, target_id=target_id) as (page, _):
            await page.evaluate(signal_js(state, note))
    except Exception as error:  # noqa: BLE001 - the indicator is advice
        log.debug("notify %s failed: %s", state, error)


def notify_url(cdp_url: str, url: str, state: str, note: str = "") -> bool:
    """`notify` for a caller without an event loop or a target id (the
    pipeline): the open tab on `url`, if any. False when there is none."""
    target_id = find_tab(cdp_url, url)
    if not target_id:
        return False
    asyncio.run(notify(cdp_url, target_id, state, note))
    return True

