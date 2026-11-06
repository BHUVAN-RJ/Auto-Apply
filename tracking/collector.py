"""Every tab in the one Chrome we own, watched until it closes.

`tools/dev_workday_recorder.py` watches Workday and keeps shapes. This keeps
characters (except secrets, see `policy.py`), watches every page that is not
off limits, and answers the three questions that tool could not:

- **where a tab came from.** `Target.targetCreated` carries an `openerId`,
  `Page.windowOpen` carries the URL that was asked for and whether a user
  gesture was in flight, and the patched `window.open` in `recorder.js`
  carries the call's own stack and the last thing the person touched. One
  row, `tab_opened`, holds all three, so "3-9 blank tabs appear" stops being
  a thing to guess at.
- **who closed it.** A tab that calls `window.close` says so from the inside,
  with a stack. A tab that simply disappears was closed from the outside -
  the person, or an extension's `chrome.tabs.remove`, which leaves no trace
  in the page. The two read differently in the log, which is the difference
  between "I close them" and "something closes them".
- **what was corrected.** `recorder.js` keeps the value a field held when it
  was entered, the value a script put there, and the value it held when it
  was left.

Alongside that it runs the **shadow**: on a page our own selector table
claims to know, `tracking/shadow.py` resolves every field read-only and
records what it *would* have filled. It writes nothing to the page. Two
writers on one form is the bug of 2026-10-11, and the live flow is not ours
to disturb.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import paths  # noqa: E402
from browser import chrome  # noqa: E402
from browser.autofill import attached  # noqa: E402
from tracking import policy, shadow  # noqa: E402
from tracking.store import Session  # noqa: E402

RECORDER_JS = (Path(__file__).resolve().parent / "recorder.js").read_text()

DRAIN = 0.5        # how often a tab's buffer is emptied
SHAPE = 6.0        # how often the page is looked at as a whole
SWEEP = 1.0        # how often the tab list is re-read
# A CDP socket can drop without a close frame - the injector died of exactly
# that on 2026-10-28. An `await` on a dead socket never returns, so one tab
# going quiet would otherwise make the tracker blind to that tab for ever
# while every other sign of life says it is running.
ASK = 20.0         # how long one evaluate may take before the tab is re-taken


def _armed() -> str:
    """The recorder, guarded so it cannot arm on a page we may not read.

    `Page.addScriptToEvaluateOnNewDocument` fires on every document the tab
    goes on to show, and whether a page is off limits is decided per URL.
    The guard is in the injected source for that reason: a tab that walks
    from a careers page into a webmail tab must go quiet by itself, without
    waiting for the next poll.
    """
    off = json.dumps(list(policy.OFF_LIMITS))
    # The predicate is named so it can be asked about on its own: a guard
    # that can only be tested by serving a page from someone else's host is
    # a guard nobody tests.
    return ("(() => {"
            f" const off = {off};"
            " window.__trkAllowed = (host) => { const h = String(host || '').toLowerCase();"
            "   return !off.some((d) => h === d || h.endsWith('.' + d)); };"
            " if (!window.__trkAllowed(location.hostname)) return;"
            f" {RECORDER_JS} }})()")


class Tabs:
    """What is known about every tab, and why each one exists."""

    def __init__(self, session: Session):
        self.session = session
        self.urls: dict[str, str] = {}
        self.openers: dict[str, str] = {}
        self.asked: dict[str, dict] = {}     # targetId -> the window.open row
        self.said_close: set[str] = set()    # tabs that closed themselves
        self.watched: set[str] = set()

    def opened(self, target: dict, reason: Optional[dict] = None) -> None:
        tid = target.get("targetId", "")
        url = target.get("url") or ""
        opener = target.get("openerId") or ""
        self.urls[tid] = url
        if opener:
            self.openers[tid] = opener
        blank = not url or url.startswith("about:")
        self.session.write({
            "kind": "tab_opened", "tab": tid[:8], "url": url if policy.trackable(url) else "",
            "host": policy.host_of(url), "blank": blank,
            "opener": opener[:8] if opener else "",
            "opener_url": self.urls.get(opener, "")[:200] if opener else "",
            # Why. The page's own `window.open` call, with its stack and the
            # last thing the person touched before it, when there was one.
            "because": reason or None,
            "not_read": policy.why_not(url),
        })

    def closed(self, tid: str) -> None:
        url = self.urls.pop(tid, "")
        self.session.write({
            "kind": "tab_closed", "tab": tid[:8], "url": url[:200],
            "blank": not url or url.startswith("about:"),
            # A tab that called `window.close` said so from the inside. One
            # that did not was closed from the outside: the person's hand, or
            # an extension, which the page cannot see either way.
            "closed_itself": tid in self.said_close,
            "by": "the page or a script in it" if tid in self.said_close else "from outside the page",
        })
        self.said_close.discard(tid)
        self.openers.pop(tid, None)
        self.watched.discard(tid)


async def ask(page, expression: str):
    """One evaluate, with a deadline. See `ASK`."""
    return await asyncio.wait_for(page.evaluate(expression), timeout=ASK)


async def follow(cdp_url: str, tid: str, url: str, tabs: Tabs) -> None:
    """One tab, until it goes away."""
    session = tabs.session
    try:
        async with attached(cdp_url, target_id=tid) as (page, _):
            await page.send("Page.enable")
            armed = _armed()
            await page.send("Page.addScriptToEvaluateOnNewDocument", {"source": armed})
            await page.evaluate(armed)
            last_shape: Optional[tuple] = None
            last_url = url
            seen_fields: set[str] = set()
            next_shape = 0.0
            while True:
                here = str(await ask(page, "location.href") or "")
                if here != last_url:
                    session.write({"kind": "went", "tab": tid[:8],
                                   "from": last_url[:200] if policy.trackable(last_url) else "",
                                   "to": here[:200] if policy.trackable(here) else "",
                                   "not_read": policy.why_not(here)})
                    last_url, last_shape, seen_fields = here, None, set()
                if not policy.readable(here):
                    await asyncio.sleep(DRAIN * 2)
                    continue

                # Tell the page which of its controls may be read, before
                # anything typed into them is drained.
                fields = json.loads(await ask(page, "window.__trkFields && window.__trkFields()")
                                    or "[]")
                fresh = [f for f in fields if f.get("ref") and f["ref"] not in seen_fields]
                if fresh:
                    secrets = [f["ref"] for f in fresh
                               if policy.secret_field(f.get("type", ""), f.get("name", ""),
                                                      f.get("id", ""), f.get("label", ""),
                                                      f.get("autocomplete", ""))]
                    await ask(
                        page,
                        f"window.__trkClassify({json.dumps(secrets)}, "
                        f"{json.dumps([f['ref'] for f in fresh])})")
                    seen_fields.update(f["ref"] for f in fresh)
                    if secrets:
                        session.write({"kind": "secrets", "tab": tid[:8], "count": len(secrets),
                                       "labels": [f.get("label", "")[:60] for f in fresh
                                                  if f["ref"] in secrets]})

                for row in json.loads(await ask(page, "window.__trkDrain && window.__trkDrain()")
                                      or "[]"):
                    row["tab"] = tid[:8]
                    if row.get("kind") == "window.close":
                        tabs.said_close.add(tid)
                    session.write(row)
                    session.count(row)

                if time.monotonic() >= next_shape:
                    next_shape = time.monotonic() + SHAPE
                    snap = await asyncio.wait_for(
                        session.snapshot(page, tid, fields), timeout=ASK)
                    if snap and snap != last_shape:
                        last_shape = snap
                        # The parallel run: what our own table would have done
                        # with this page. Reads; never writes.
                        await asyncio.wait_for(
                            shadow.score(page, here, session, tid), timeout=ASK)
            # unreachable
    except asyncio.CancelledError:
        raise
    except Exception as error:  # noqa: BLE001 - the tab closed, or Chrome did
        why = "the tab stopped answering" if isinstance(error, asyncio.TimeoutError) \
            else str(error)[:160]
        session.write({"kind": "tab_detached", "tab": tid[:8], "why": why})
        print(f"  lost {tid[:8]}: {why}")
    finally:
        # A beat first, so a tab that cannot be attached at all is retried
        # rather than spun on.
        await asyncio.sleep(2.0)
        # Hand the tab back to the sweep. Without this a tab is followed once
        # and never again, so a dropped socket reads exactly like a person who
        # stopped clicking.
        tabs.watched.discard(tid)


def _pages(cdp_url: str) -> list[dict]:
    import urllib.request
    try:
        with urllib.request.urlopen(f"{cdp_url}/json", timeout=5) as response:
            return [t for t in json.loads(response.read()) if t.get("type") == "page"]
    except Exception:  # noqa: BLE001 - Chrome went away
        return []


async def events(cdp_url: str, tabs: Tabs) -> None:
    """The browser's own view: every target created, renamed and destroyed,
    and every `window.open` the browser itself reports."""
    import urllib.request

    import websockets
    with urllib.request.urlopen(f"{cdp_url}/json/version", timeout=5) as response:
        socket_url = json.loads(response.read())["webSocketDebuggerUrl"]
    async with websockets.connect(socket_url, max_size=None) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Target.setDiscoverTargets",
                                  "params": {"discover": True}}))
        while True:
            message = json.loads(await ws.recv())
            method, params = message.get("method"), message.get("params") or {}
            info = params.get("targetInfo") or {}
            if method == "Target.targetCreated" and info.get("type") == "page":
                tabs.opened(info)
            elif method == "Target.targetInfoChanged" and info.get("type") == "page":
                tid = info.get("targetId", "")
                was, now = tabs.urls.get(tid), info.get("url") or ""
                if was is not None and now != was:
                    tabs.urls[tid] = now
                    tabs.session.write({"kind": "tab_url", "tab": tid[:8],
                                        "was": was[:200] if policy.trackable(was) else "",
                                        "now": now[:200] if policy.trackable(now) else ""})
            elif method == "Target.targetDestroyed":
                tid = params.get("targetId", "")
                if tid in tabs.urls:
                    tabs.closed(tid)


async def watch(cdp_url: str, root: Path) -> None:
    session = Session(root)
    tabs = Tabs(session)
    print(f"tracking every tab on {cdp_url}\n  -> {session.dir}")
    print("  off limits (never read): mail, banking, password managers")
    asyncio.create_task(events(cdp_url, tabs))
    try:
        while True:
            for target in _pages(cdp_url):
                tid = target.get("id") or target.get("targetId") or ""
                if not tid or tid in tabs.watched:
                    continue
                tabs.watched.add(tid)
                url = target.get("url") or ""
                tabs.urls.setdefault(tid, url)
                reason = policy.why_not(url)
                print(f"  watch {tid[:8]} {policy.host_of(url) or '(blank)'}"
                      + (f"  [{reason}: counted, not read]" if reason else ""))
                asyncio.create_task(follow(cdp_url, tid, url, tabs))
            await asyncio.sleep(SWEEP)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        session.close()


def main(argv: Optional[list[str]] = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)
    port = args.port or chrome.port()
    root = Path(args.out) if args.out else paths.DATA / "tracking"
    root.mkdir(parents=True, exist_ok=True)
    try:
        asyncio.run(watch(chrome.cdp_url(port), root))
    except KeyboardInterrupt:
        print("\nstopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
