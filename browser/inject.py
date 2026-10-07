"""Put the capture script into pages over CDP, so no extension is installed.

`capture/content.js` was written as an extension content script, and
installing an extension is the one step of the one-click thesis that has no
clean answer: `--load-extension` is gone from branded Chrome since 137,
policy force-install is ignored on unmanaged Macs, and the Web Store means a
review queue plus a click. The Chrome the fill runs in is ours already
(browser/chrome.py), so the same script can simply be evaluated in its tabs
from here, over the debugging port it is listening on anyway.

This replaces both halves of the extension:

- content.js is evaluated in every tab that is on Jobright, or that was
  opened from Jobright, or that navigated away from it. That is what
  background.js decided with `chrome.tabs`; here it is decided from
  `Target.*` events, which carry the opener and every URL change.
- It is evaluated again after every load. The script's own guard makes a
  second copy in the same document a no-op, and its URL poll follows
  single-page navigations without a reload.

The script runs in the page's main world, not an isolated one, so it must
not rely on `chrome.*` APIs; content.js never did. CORS on the server is
already open (server/app.py), so its fetches to 8787 work from any origin.

Run it alone with `python -m browser.inject`; it launches or reuses the
Auto-Apply Chrome and stays attached until Chrome quits or Ctrl-C. The one
click made here is Jobright's Autofill, through browser/autofill.py and its
deny-list, as soon as an employer tab has loaded. Nothing here submits or
closes anything.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import urllib.error
import urllib.request
from itertools import count
from pathlib import Path
from typing import Optional

from . import autofill, chrome

log = logging.getLogger("inject")

SERVER = os.environ.get("AUTOPILOT_SERVER_URL", "http://127.0.0.1:8787").rstrip("/")
# The page calls this with a JSON request and gets `__autopilotReply` back.
# Pages that run the script as an extension content script never see it.
BINDING = "__autopilotRequest"
# A bridge "path" that opens a tab instead of calling the server.
OPEN_PATH = "/__open"
# The page asks for its own tab to go once the job is queued.
CLOSE_PATH = "/__close"
SERVER_ORIGIN = SERVER

CONTENT_JS = Path(__file__).resolve().parent.parent / "capture" / "content.js"
# The Simplify new-grad list's own GitHub page gets a script of its own: the
# tracker, which draws a status column over the tables and works one day of
# postings at a time (`capture/tracker.js`). It is not a posting, so the
# banner has nothing to say about it, and the tracker never acts by itself.
TRACKER_JS = Path(__file__).resolve().parent.parent / "capture" / "tracker.js"
LIST_PAGE = re.compile(r"^https://github\.com/[^/]+/New-Grad-Positions(/|$|\?|#)", re.I)
SOURCE_HOSTS = ("jobright.ai",)
# Side by side: the source list on the left half of the screen, the job its
# Apply opens on the right (2026-10-07, asked for). A true split inside one
# tab is not available - `jobs.ashbyhq.com` and Workday both answer
# `x-frame-options: DENY`, Greenhouse sends `default-src 'self'`, and even
# our own page cannot be framed on an https site because Chrome refuses it a
# private address - so the two halves are two windows.
SPLIT_ENV = "AUTOPILOT_SPLIT"
# Left for the list, right for the job. A gap of nothing: the two windows
# meet, which is what "split" is asked to look like.
SPLIT_SHARE = 0.5
# Jobright's own extension opens "Apply with autofill" in a tab it creates
# itself, so there is no opener and no Page.windowOpen to tie it to the
# posting; the URL it opens is tagged with the posting id instead.
SOURCE_MARKS = ("jr_id=",)
# An application form opened by hand is still a form: an Ashby posting found
# through a friend, a Workday portal reached from a company's own careers
# page. Those tabs have no opener and no `jr_id`, so nothing marked them and
# the banner never appeared - no verdict, and no way to put the tailored
# resume on the slot. They get the script on their host alone. They are not
# `marked`: nothing auto-queues them, presses Autofill in them, or closes
# them; the person clicks.
ATS_HOSTS = (
    "myworkdayjobs.com", "myworkdaysite.com", "wd1.myworkdayjobs.com",
    "greenhouse.io", "ashbyhq.com", "lever.co", "smartrecruiters.com",
    "bamboohr.com", "workable.com", "icims.com", "jobvite.com", "taleo.net",
    "successfactors.com", "oraclecloud.com", "eightfold.ai", "dover.com",
)
# Jobright's Autofill is pressed the moment an employer tab has loaded, by
# code, before any job is queued or approved: the human opened the form to
# apply, and waiting for the pipeline to press it was the delay they saw.
AUTOFILL_ON_OPEN = "AUTOPILOT_AUTOFILL_ON_OPEN"


def from_source(url: str) -> bool:
    """Whether a URL is on one of the sites whose Apply buttons we follow."""
    try:
        host = url.split("://", 1)[1].split("/", 1)[0].split("@")[-1].split(":")[0].lower()
    except IndexError:
        return False
    return any(host == h or host.endswith("." + h) for h in SOURCE_HOSTS)


def is_ats(url: str) -> bool:
    """Whether a URL is an application form on a known ATS, however it was
    opened. `ats.detect` is the browser model's note picker and deliberately
    narrower (it matches Oracle's candidate-experience path only); this is
    the banner's question, which is only ever about the host."""
    try:
        host = url.split("://", 1)[1].split("/", 1)[0].split("@")[-1].split(":")[0].lower()
    except IndexError:
        return False
    return any(host == h or host.endswith("." + h) for h in ATS_HOSTS)


def tagged(url: str) -> bool:
    """Whether a URL carries a source site's own tag, like Jobright's jr_id."""
    query = url.split("?", 1)[1] if "?" in url else ""
    return any(mark in query for mark in SOURCE_MARKS)


def is_list_page(url: str) -> bool:
    """The job list the tracker is drawn on, rather than a posting."""
    return bool(LIST_PAGE.match(url or ""))


def is_web(url: str) -> bool:
    return url.startswith("http://") or url.startswith("https://")


class Page:
    """The page interface autofill.run wants, over the injector's one socket."""

    def __init__(self, injector: "Injector", session: str):
        self.injector = injector
        self.session = session

    async def send(self, method: str, params: Optional[dict] = None) -> dict:
        return await self.injector.send(method, params, self.session)

    async def evaluate(self, expression: str):
        result = await self.send("Runtime.evaluate", {
            "expression": expression, "returnByValue": True, "awaitPromise": True,
        })
        if "exceptionDetails" in result:
            raise RuntimeError(result["exceptionDetails"].get("text", "script threw"))
        return result.get("result", {}).get("value")


class Injector:
    """One CDP connection to the browser; page sessions multiplexed over it."""

    def __init__(self, ws_url: str, script: str, tracker: str = ""):
        self.ws_url = ws_url
        self.script = script
        # The list page's own script; empty means it gets the banner's.
        self.tracker = tracker
        self.ids = count(1)
        self.pending: dict[int, asyncio.Future] = {}
        self.socket = None
        # targetId -> last known URL, and the set of targets that came from
        # a source site and get the script wherever they land.
        self.urls: dict[str, str] = {}
        self.marked: set[str] = set()
        self.sessions: dict[str, str] = {}  # sessionId -> targetId
        # URLs a source page asked to open in a new window. Jobright opens
        # Apply with `noopener`, so the new target carries no openerId; the
        # Page.windowOpen event on the opener is what still ties them.
        self.opened: set[str] = set()
        # (targetId, URL) pairs where Autofill has been pressed, so a reload
        # or a second load event never presses twice.
        self.autofilled: set[tuple[str, str]] = set()
        # Off by default: the employer tab that Apply opens exists to be
        # screened and queued, then closes itself; the fill opens its own
        # tab on approve and presses Autofill there.
        self.autofill_on_open = os.environ.get(AUTOFILL_ON_OPEN, "0") == "1"
        # Side by side. `split` is the feature; `job_window` is the window
        # the right-hand half lives in, reused for every job so a morning of
        # applying does not end in thirty windows; `screen` is the usable
        # desktop, read once off a real page because CDP does not report it.
        self.split = os.environ.get(SPLIT_ENV, "1") != "0"
        # The one tab on the right, reused for every job so a morning of
        # applying does not end in thirty windows, and a count per URL of
        # the tabs we are about to open there. The count is claimed *before*
        # the target is made: `run` dispatches every event as a task, so the
        # `targetCreated` for our own new tab can be handled before
        # `createTarget` has even replied, and a guard set afterwards is set
        # too late - three copies of one page, measured on the live browser.
        self.job_tab: Optional[str] = None
        self.splitting: dict[str, int] = {}
        self.screen: Optional[dict] = None
        self.tiled: set[str] = set()

    async def send(self, method: str, params: Optional[dict] = None, session: Optional[str] = None) -> dict:
        message_id = next(self.ids)
        message = {"id": message_id, "method": method, "params": params or {}}
        if session:
            message["sessionId"] = session
        future = asyncio.get_running_loop().create_future()
        self.pending[message_id] = future
        await self.socket.send(json.dumps(message))
        return await future

    def wanted(self, target_id: str, url: str) -> bool:
        """Every web page gets the script; the app's own page does not. The
        browser is the person's job-hunting window, so a posting can be on
        any host - a company's own careers page, a startup's one-off form -
        and a host list could only ever guess. What a page nothing pointed
        at a job gets is `cold`: the badge and no request at all."""
        if not is_web(url):
            return False
        return not (url == SERVER_ORIGIN or url.startswith(f"{SERVER_ORIGIN}/"))

    def cold(self, target_id: str, url: str) -> bool:
        """Whether this tab is a page nobody said was a job. Cold pages draw
        the badge, read nothing and call nothing until it is clicked."""
        return not (from_source(url) or target_id in self.marked or is_ats(url))

    async def inject(self, session: str, target_id: str, why: str) -> None:
        url = self.urls.get(target_id, "")
        if not self.wanted(target_id, url):
            return
        if self.tracker and is_list_page(url):
            # Not a posting: a list of them, with its own overlay. Nothing
            # is read from it until the person presses a button on it.
            await self.evaluate_script(session, self.tracker, url, "tracker")
            return
        # A tab nothing marked was opened by the person, not by an Apply
        # button: the banner screens and offers, but never counts down.
        hand = target_id not in self.marked and not from_source(url)
        cold = self.cold(target_id, url)
        script = (
            f"window.__autopilotHandOpened={'true' if hand else 'false'};\n"
            f"window.__autopilotCold={'true' if cold else 'false'};\n{self.script}"
        )
        await self.evaluate_script(session, script, url, why)

    async def evaluate_script(self, session: str, script: str, url: str, why: str) -> None:
        try:
            result = await self.send(
                "Runtime.evaluate",
                {"expression": script, "awaitPromise": False, "returnByValue": True},
                session,
            )
        except Exception as error:  # noqa: BLE001 - a tab mid-navigation is not fatal
            log.warning("inject failed on %s: %s", url, error)
            return
        detail = result.get("exceptionDetails")
        if detail:
            log.warning("script threw on %s: %s", url, detail.get("text"))
        else:
            log.info("injected (%s) %s", why, url)

    async def on_event(self, message: dict) -> None:
        method = message.get("method")
        params = message.get("params", {})
        session = message.get("sessionId")

        if method == "Target.targetCreated":
            info = params["targetInfo"]
            log.debug("created %s", json.dumps(info))
            self.urls[info["targetId"]] = info.get("url", "")
            opener = info.get("openerId")
            if opener and from_source(self.urls.get(opener, "")):
                self.marked.add(info["targetId"])
                log.info("marked new tab from %s", self.urls[opener])
                await self.split_out(info["targetId"], info.get("url", ""))
            elif info.get("type") == "page" and (info.get("url") in self.opened or tagged(info.get("url", ""))):
                self.opened.discard(info["url"])
                self.marked.add(info["targetId"])
                log.info("marked new window %s", info["url"])
                await self.split_out(info["targetId"], info.get("url", ""))

        elif method == "Target.targetInfoChanged":
            info = params["targetInfo"]
            log.debug("changed %s", json.dumps(info))
            target_id = info["targetId"]
            previous = self.urls.get(target_id, "")
            url = info.get("url", "")
            if (from_source(previous) or tagged(url)) and url and not from_source(url) \
                    and target_id not in self.marked:
                # Same-tab navigation off Jobright: the Apply that does not
                # open a new tab. Or a tagged URL arriving in a tab that
                # was created blank.
                self.marked.add(target_id)
                log.info("marked tab leaving %s for %s", previous, url)
            self.urls[target_id] = url

        elif method == "Target.targetDestroyed":
            target_id = params["targetId"]
            self.urls.pop(target_id, None)
            self.marked.discard(target_id)
            self.tiled.discard(target_id)
            if target_id == self.job_tab:
                # It closed itself once the job was queued; the next one
                # opens a window again rather than reusing a gone tab.
                self.job_tab = None

        elif method == "Target.attachedToTarget":
            info = params["targetInfo"]
            if info.get("type") != "page":
                return
            new_session = params["sessionId"]
            target_id = info["targetId"]
            self.sessions[new_session] = target_id
            self.urls.setdefault(target_id, info.get("url", ""))
            await self.send("Page.enable", session=new_session)
            await self.send("Runtime.enable", session=new_session)
            await self.send("Runtime.addBinding", {"name": BINDING}, session=new_session)
            # A tab that is already open and loaded fires no load event.
            await self.inject(new_session, target_id, "attach")
            await self.tile(new_session, target_id)

        elif method == "Target.detachedFromTarget":
            self.sessions.pop(params.get("sessionId", ""), None)

        elif method == "Page.windowOpen" and session in self.sessions:
            if from_source(self.urls.get(self.sessions[session], "")):
                url = params.get("url", "")
                # The target may already exist by the time this arrives.
                for target_id, known in self.urls.items():
                    if known == url and target_id not in self.marked:
                        self.marked.add(target_id)
                        log.info("marked open window %s", url)
                        break
                else:
                    self.opened.add(url)

        elif method == "Runtime.bindingCalled" and params.get("name") == BINDING:
            await self.relay(session, params.get("payload", ""))

        elif method == "Page.loadEventFired" and session in self.sessions:
            await self.inject(session, self.sessions[session], "load")
            self.maybe_autofill(session, self.sessions[session])

    def maybe_autofill(self, session: str, target_id: str) -> None:
        """Press Autofill in a freshly loaded employer tab, once, in the
        background; the event loop must not wait on Jobright."""
        url = self.urls.get(target_id, "")
        key = (target_id, url.split("#", 1)[0])
        if not (self.autofill_on_open and target_id in self.marked and is_web(url)
                and not from_source(url) and not url.startswith(SERVER_ORIGIN)
                and key not in self.autofilled):
            return
        self.autofilled.add(key)
        asyncio.create_task(self.press(session, target_id, url))

    async def press(self, session: str, target_id: str, url: str) -> None:
        page = Page(self, session)
        await self.signal(page, "working", "Pressing Jobright autofill")
        try:
            result = await autofill.run(page, target_id)
        except Exception as error:  # noqa: BLE001 - the fill presses it later
            log.warning("autofill on %s failed: %s", url, error)
            await self.signal(page, "error", f"Autofill failed: {error}")
            return
        log.info("%s on %s", result.summary(), url)
        if result.clicked:
            await self.signal(page, "done", result.summary())
        else:
            await self.signal(page, "idle", result.summary())
        # Autofill over (or absent) is the trigger for the browser agent on
        # a job already in autopilot; the server decides what that means for
        # this URL. A job not in autopilot yet is the banner's to capture,
        # and the pipeline starts the fill itself.
        reached, status, body = await asyncio.to_thread(post, "/autofilled", {"url": url, "note": result.summary()})
        if not reached or status != 200 or not isinstance(body, dict):
            log.warning("/autofilled on %s: %s %s", url, status, body)
            return
        action = body.get("action")
        log.info("after autofill on %s: %s (job %s)", url, action, body.get("id"))
        if action == "fill":
            await self.signal(page, "working", "Browser agent is taking over")
        elif action == "pipeline":
            await self.signal(page, "working", "Tailoring the resume")
        elif action == "running":
            await self.signal(page, "working", "Browser agent is already on this form")

    async def signal(self, page: Page, state: str, note: str = "") -> None:
        try:
            await page.evaluate(autofill.signal_js(state, note))
        except Exception as error:  # noqa: BLE001 - the indicator is advice
            log.debug("signal %s failed: %s", state, error)

    async def relay(self, session: str, payload: str) -> None:
        """Make one request to the server for the page and hand back the reply.

        Only POSTs to the local server on the page's behalf; the path comes
        from our own script. The page's origin, CSP, and workers are not
        involved, which is the point: an ATS page's service worker has
        swallowed a direct fetch before.
        """
        try:
            request = json.loads(payload)
            request_id = request["id"]
        except (ValueError, KeyError, TypeError):
            log.warning("bad bridge payload: %.100s", payload)
            return
        path = request.get("path", "")
        if path == CLOSE_PATH:
            # The reply goes first: the page is gone once the tab is.
            ok, status, body = self.closable(session)
            await self.reply(session, request_id, ok, status, body)
            if status == 200:
                await self.close_tab(session)
            return
        if path == OPEN_PATH:
            # Not a server call: the page asks for a tab. window.open from a
            # script evaluated into a page is at the mercy of the site's
            # popup handling; the browser itself is not.
            body = request.get("body") or {}
            ok, status, body = await self.open_tab(str(body.get("url", "")), body.get("focus", True) is not False)
        else:
            ok, status, body = await asyncio.to_thread(post, path, request.get("body"))
        await self.reply(session, request_id, ok, status, body)

    async def reply(self, session: str, request_id: object, ok: bool, status: int, body: object) -> None:
        reply = f"window.__autopilotReply({json.dumps(request_id)}, {json.dumps(ok)}, {status}, {json.dumps(body)})"
        try:
            await self.send("Runtime.evaluate", {"expression": reply}, session)
        except Exception as error:  # noqa: BLE001 - the page may be gone
            log.warning("reply failed: %s", error)

    def closable(self, session: str) -> tuple[bool, int, object]:
        """Only the employer tab that asked, and only one Apply opened: never
        a Jobright tab, never the review page, never the browser. A fill
        never asks; its tab stays open on the finished form."""
        target_id = self.sessions.get(session)
        url = self.urls.get(target_id or "", "")
        if not target_id or target_id not in self.marked or not is_web(url) or from_source(url) \
                or url == SERVER_ORIGIN or url.startswith(f"{SERVER_ORIGIN}/"):
            return True, 403, {"detail": "only an employer tab opened from Jobright may close itself"}
        return True, 200, {"ok": True}

    async def close_tab(self, session: str) -> None:
        target_id = self.sessions.get(session)
        if not target_id:
            return
        try:
            await self.send("Target.closeTarget", {"targetId": target_id})
            log.info("closed queued tab %s", self.urls.get(target_id, ""))
        except Exception as error:  # noqa: BLE001 - a tab left open is harmless
            log.warning("close failed: %s", error)

    # -- side by side ------------------------------------------------------

    async def desktop(self, session: str) -> Optional[dict]:
        """The usable screen, read off a real page.

        CDP has no display API; `window.screen` does, and it already knows
        about the menu bar and the Dock (`avail*`). Read once and kept: the
        person may move the window, and re-reading would fight them.
        """
        if self.screen is not None:
            return self.screen
        try:
            got = await self.send("Runtime.evaluate", {
                "expression": "({w: screen.availWidth, h: screen.availHeight,"
                              " x: screen.availLeft || 0, y: screen.availTop || 0})",
                "returnByValue": True,
            }, session)
        except Exception as error:  # noqa: BLE001 - no tiling, nothing else
            log.debug("could not read the screen: %s", error)
            return None
        value = (got.get("result") or {}).get("value") or {}
        if not (value.get("w") and value.get("h")):
            return None
        self.screen = value
        return self.screen

    async def place(self, target_id: str, half: str, session: str) -> None:
        """Put this target's window on the left or right half of the screen."""
        screen = await self.desktop(session)
        if not screen:
            return
        full = int(screen["w"])
        width = int(full * SPLIT_SHARE)
        if half == "right":
            # The right half takes the rounding, so the two always meet.
            left, width = int(screen["x"]) + width, full - width
        else:
            left = int(screen["x"])
        try:
            window = await self.send("Browser.getWindowForTarget", {"targetId": target_id})
            await self.send("Browser.setWindowBounds", {
                "windowId": window["windowId"],
                # A maximized or fullscreen window refuses new bounds until
                # it is normal again, and says so with an error.
                "bounds": {"windowState": "normal"},
            })
            await self.send("Browser.setWindowBounds", {
                "windowId": window["windowId"],
                "bounds": {"left": left, "top": int(screen["y"]), "width": width,
                           "height": int(screen["h"]), "windowState": "normal"},
            })
            if half == "right":
                self.job_window = window["windowId"]
        except Exception as error:  # noqa: BLE001 - the tab still works untiled
            log.debug("could not place the %s window: %s", half, error)

    async def split_out(self, target_id: str, url: str) -> None:
        """Move a job Apply opened into the right-hand window.

        Chrome has no "move this tab to that window", so the job lives in a
        tab of our own on the right and this navigates it there, closing the
        one Jobright opened. That is safe here and nowhere else: the tab is
        seconds old, it carries the posting's `jr_id` so the page it lands
        on is marked the same way, and nothing has been typed into it - it
        exists to be screened, queued, and closed by its own page.

        Reusing one tab rather than opening a window per job is deliberate:
        `Target.createTarget` cannot say which window to open in, so every
        job would otherwise get a window of its own.
        """
        if not (self.split and is_web(url)) or from_source(url):
            return
        if self.splitting.get(url):
            # Ours: the tab we asked for, arriving as an ordinary new target.
            self.splitting[url] -= 1
            if not self.splitting[url]:
                self.splitting.pop(url, None)
            self.job_tab = target_id
            self.marked.add(target_id)
            self.tiled.add(target_id)
            return
        if target_id in self.tiled or target_id == self.job_tab:
            return
        self.tiled.add(target_id)
        try:
            if self.job_tab and self.job_tab in self.urls:
                session = next((s for s, t in self.sessions.items() if t == self.job_tab), None)
                if session:
                    await self.send("Page.navigate", {"url": url}, session)
                    self.urls[self.job_tab] = url
                else:
                    self.job_tab = None
            if not self.job_tab:
                self.splitting[url] = self.splitting.get(url, 0) + 1
                try:
                    await self.send("Target.createTarget", {
                        "url": url, "newWindow": True, "background": True,
                    })
                except Exception:
                    self.splitting[url] -= 1
                    if not self.splitting[url]:
                        self.splitting.pop(url, None)
                    raise
        except Exception as error:  # noqa: BLE001 - leave the tab where it is
            log.debug("could not open the job window: %s", error)
            return
        try:
            await self.send("Target.closeTarget", {"targetId": target_id})
        except Exception as error:  # noqa: BLE001 - two tabs is survivable
            log.debug("could not close the tab we replaced: %s", error)
        log.info("job moved to the right-hand window: %s", url[:80])

    async def tile(self, session: str, target_id: str) -> None:
        """Put a newly attached tab on its side of the screen.

        The list stays where the person put it the first time it is seen;
        the job window is placed every time, because it is reused and a
        reused window has already been somewhere.
        """
        if not self.split:
            return
        url = self.urls.get(target_id, "")
        if from_source(url):
            if target_id not in self.tiled:
                self.tiled.add(target_id)
                await self.place(target_id, "left", session)
            return
        if target_id == self.job_tab:
            await self.place(target_id, "right", session)
            # The person is reading the list, not this. Give the front back.
            await self.focus_source()

    async def focus_source(self) -> None:
        """Bring the list back to the front, if one is open."""
        source = next((t for t, u in self.urls.items() if from_source(u)), None)
        if source is None:
            return
        try:
            await self.send("Target.activateTarget", {"targetId": source})
        except Exception as error:  # noqa: BLE001 - focus is a courtesy
            log.debug("could not bring the list back: %s", error)

    async def open_tab(self, url: str, focus: bool = True) -> tuple[bool, int, object]:
        """The review page, reused when open. `focus` False points it at
        the job and leaves the active tab alone (a new one is created in
        the background): a queued job is not a reason to leave the posting;
        the fill's own tab is what comes to the front, on approve."""
        if not (is_web(url) and (url == SERVER_ORIGIN or url.startswith(f"{SERVER_ORIGIN}/"))):
            return True, 403, {"detail": "only the review page may be opened this way"}
        try:
            existing = next(
                (target_id for target_id, known_url in self.urls.items()
                 if known_url == f"{SERVER_ORIGIN}/" or known_url.startswith(f"{SERVER_ORIGIN}/#")),
                None,
            )
            if existing is not None:
                session = next(
                    (session_id for session_id, target_id in self.sessions.items()
                     if target_id == existing),
                    None,
                )
                if session is not None:
                    await self.send("Page.navigate", {"url": url}, session)
                if focus:
                    await self.send("Target.activateTarget", {"targetId": existing})
                return True, 200, {"ok": True, "reused": True}
            await self.send("Target.createTarget", {"url": url, "background": not focus})
            return True, 200, {"ok": True, "reused": False}
        except Exception as error:  # noqa: BLE001 - reported to the page
            return True, 502, {"detail": str(error)}

    async def run(self) -> None:
        """Stay attached for as long as Chrome is up.

        The browser socket has dropped mid-session with no close frame
        (once, thirty seconds after a new tab; cause unknown, the fill's
        own attach is the suspect). Every open tab lost its banner and the
        process sat dead until someone looked. So: reconnect while the
        debugging port still answers, and only give up when it does not.
        """
        from websockets.asyncio.client import connect  # browser-use's dependency
        from websockets.exceptions import ConnectionClosed

        delay = 1.0
        while True:
            try:
                async with connect(self.ws_url, max_size=None, ping_interval=None) as socket:
                    self.socket = socket
                    self.pending.clear()
                    self.sessions.clear()
                    reader = asyncio.create_task(self.read())
                    await self.send("Target.setDiscoverTargets", {"discover": True})
                    await self.send(
                        "Target.setAutoAttach",
                        {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True},
                    )
                    log.info("attached; watching tabs")
                    delay = 1.0
                    await reader
            except (ConnectionClosed, OSError) as error:
                for future in self.pending.values():
                    if not future.done():
                        future.set_exception(error)
                self.pending.clear()
                cdp_url = self.ws_url.split("/devtools/", 1)[0].replace("ws://", "http://")
                if not chrome.is_listening(cdp_url):
                    log.info("Chrome is gone (%s); stopping", error)
                    return
                log.warning("browser socket dropped (%s); reattaching in %.0fs", error, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 10.0)

    async def read(self) -> None:
        async for raw in self.socket:
            message = json.loads(raw)
            if "id" in message:
                future = self.pending.pop(message["id"], None)
                if future is None:
                    continue
                if "error" in message:
                    future.set_exception(RuntimeError(message["error"].get("message")))
                else:
                    future.set_result(message.get("result", {}))
            else:
                # Handlers await sends, and a send needs this loop to keep
                # reading, so events are handled as tasks.
                asyncio.create_task(self.on_event(message))


def post(path: str, body: object) -> tuple[bool, int, object]:
    """POST JSON to the local server. Returns (reached, status, decoded body)."""
    if not path.startswith("/"):
        return False, 0, "bad path"
    data = json.dumps(body).encode()
    request = urllib.request.Request(
        f"{SERVER}{path}", data=data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            return True, response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        try:
            return True, error.code, json.loads(error.read() or b"null")
        except ValueError:
            return True, error.code, {"detail": str(error)}
    except Exception as error:  # noqa: BLE001 - server down, timeout
        return False, 0, f"Is the server running? {error}"


def browser_ws(cdp_url: str) -> str:
    with urllib.request.urlopen(f"{cdp_url}/json/version", timeout=5) as response:
        return json.loads(response.read())["webSocketDebuggerUrl"]


async def open_tab(cdp_url: str, url: str) -> None:
    request = urllib.request.Request(f"{cdp_url}/json/new?{url}", method="PUT")
    with urllib.request.urlopen(request, timeout=5):
        pass


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--open", metavar="URL", help="open this URL in a new tab once attached")
    parser.add_argument("--script", type=Path, default=CONTENT_JS)
    parser.add_argument("--tracker", type=Path, default=TRACKER_JS,
                        help="the overlay for the job list's own page")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    from .fill import profile_dir  # lazy: fill.py is the module that owns the profile

    cdp_url = chrome.ensure(profile_dir())
    # content.js keeps 8787 as the extension default.  The injected copy uses
    # the same configurable server as this bridge, so another local service
    # occupying that port does not split browser requests across two apps.
    def read(path: Path) -> str:
        return path.read_text().replace("http://127.0.0.1:8787", SERVER) if path.exists() else ""

    script = read(args.script)
    injector = Injector(browser_ws(cdp_url), script, read(args.tracker))

    async def go() -> None:
        if args.open:
            await open_tab(cdp_url, args.open)
        await injector.run()

    try:
        asyncio.run(go())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
