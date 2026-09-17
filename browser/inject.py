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
Auto-Apply Chrome and stays attached until Chrome quits or Ctrl-C. Nothing
here clicks, submits, or closes anything: it only adds the banner.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import urllib.error
import urllib.request
from itertools import count
from pathlib import Path
from typing import Optional

from . import chrome

log = logging.getLogger("inject")

SERVER = "http://127.0.0.1:8787"
# The page calls this with a JSON request and gets `__autopilotReply` back.
# Pages that run the script as an extension content script never see it.
BINDING = "__autopilotRequest"

CONTENT_JS = Path(__file__).resolve().parent.parent / "capture" / "content.js"
SOURCE_HOSTS = ("jobright.ai",)
# Jobright's own extension opens "Apply with autofill" in a tab it creates
# itself, so there is no opener and no Page.windowOpen to tie it to the
# posting; the URL it opens is tagged with the posting id instead.
SOURCE_MARKS = ("jr_id=",)


def from_source(url: str) -> bool:
    """Whether a URL is on one of the sites whose Apply buttons we follow."""
    try:
        host = url.split("://", 1)[1].split("/", 1)[0].split("@")[-1].split(":")[0].lower()
    except IndexError:
        return False
    return any(host == h or host.endswith("." + h) for h in SOURCE_HOSTS)


def tagged(url: str) -> bool:
    """Whether a URL carries a source site's own tag, like Jobright's jr_id."""
    query = url.split("?", 1)[1] if "?" in url else ""
    return any(mark in query for mark in SOURCE_MARKS)


def is_web(url: str) -> bool:
    return url.startswith("http://") or url.startswith("https://")


class Injector:
    """One CDP connection to the browser; page sessions multiplexed over it."""

    def __init__(self, ws_url: str, script: str):
        self.ws_url = ws_url
        self.script = script
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
        return is_web(url) and (from_source(url) or target_id in self.marked)

    async def inject(self, session: str, target_id: str, why: str) -> None:
        url = self.urls.get(target_id, "")
        if not self.wanted(target_id, url):
            return
        try:
            result = await self.send(
                "Runtime.evaluate",
                {"expression": self.script, "awaitPromise": False, "returnByValue": True},
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
            elif info.get("type") == "page" and (info.get("url") in self.opened or tagged(info.get("url", ""))):
                self.opened.discard(info["url"])
                self.marked.add(info["targetId"])
                log.info("marked new window %s", info["url"])

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
        ok, status, body = await asyncio.to_thread(post, request.get("path", ""), request.get("body"))
        reply = f"window.__autopilotReply({json.dumps(request_id)}, {json.dumps(ok)}, {status}, {json.dumps(body)})"
        try:
            await self.send("Runtime.evaluate", {"expression": reply}, session)
        except Exception as error:  # noqa: BLE001 - the page may be gone
            log.warning("reply failed: %s", error)

    async def run(self) -> None:
        from websockets.asyncio.client import connect  # browser-use's dependency

        async with connect(self.ws_url, max_size=None) as socket:
            self.socket = socket
            reader = asyncio.create_task(self.read())
            await self.send("Target.setDiscoverTargets", {"discover": True})
            await self.send(
                "Target.setAutoAttach",
                {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True},
            )
            log.info("attached; watching tabs")
            await reader

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
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    from .fill import profile_dir  # lazy: fill.py is the module that owns the profile

    cdp_url = chrome.ensure(profile_dir())
    injector = Injector(browser_ws(cdp_url), args.script.read_text())

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
