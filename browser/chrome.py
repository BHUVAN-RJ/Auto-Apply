"""Own the Chrome process, so browser-use only ever attaches to it.

When browser-use launches Chrome itself it also kills it: its local-browser
watchdog answers every stop event with a kill, and `keep_alive` is not
consulted on that path. That is how the window holding the completed form kept
closing the moment the agent finished, with `browser.stop()` and
`keep_alive=True` both in place and both apparently honoured.

The way out is to never let browser-use own the process. Chrome is started
here, detached from this Python process, on a fixed debugging port against the
Auto-Apply profile. browser-use is handed a CDP URL and treats the browser as
remote: stopping the session disconnects and nothing more. A later run finds
the same Chrome still listening and reuses it, so each fill opens in the same
window, with the same logins and extensions.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Optional

DEFAULT_PORT = 9333
PORT_ENV = "AUTOPILOT_CDP_PORT"

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
BRAVE = "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"
EXECUTABLE_ENV = "AUTOPILOT_BROWSER"

# How long a fresh Chrome gets to open its debugging port. It is normally well
# under two seconds; a cold start on a slow disk has been seen take ten.
LAUNCH_TIMEOUT = 30.0


class ChromeError(RuntimeError):
    pass


def port() -> int:
    return int(os.environ.get(PORT_ENV, DEFAULT_PORT))


def cdp_url(port_number: int) -> str:
    return f"http://127.0.0.1:{port_number}"


def find_browser() -> Optional[str]:
    override = os.environ.get(EXECUTABLE_ENV, "").strip()
    if override:
        return override
    for candidate in (CHROME, BRAVE):
        if Path(candidate).exists():
            return candidate
    return None


def is_listening(url: str, timeout: float = 1.0) -> bool:
    """Whether a Chromium debugging endpoint answers at this URL."""
    try:
        with urllib.request.urlopen(f"{url}/json/version", timeout=timeout) as response:
            json.loads(response.read())
        return True
    except Exception:  # noqa: BLE001 - any failure means nothing usable is there
        return False


def launch_args(executable: str, directory: Path, port_number: int, headless: bool = False) -> list[str]:
    args = [
        executable,
        f"--remote-debugging-port={port_number}",
        # CDP rejects websocket connections from unexpected origins by default,
        # and browser-use's client does not always send the one Chrome wants.
        "--remote-allow-origins=*",
        f"--user-data-dir={directory}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate",
    ]
    if headless:
        args.append("--headless=new")
    return args


def launch(directory: Path, port_number: int, headless: bool = False) -> str:
    """Start Chrome detached from this process and wait for its debug port."""
    executable = find_browser()
    if executable is None:
        raise ChromeError(
            f"no Chrome or Brave in /Applications; set {EXECUTABLE_ENV} to the binary"
        )
    directory.mkdir(parents=True, exist_ok=True)

    # Chrome writes GPU and updater chatter to stderr whether or not anything
    # is wrong; it goes to a log rather than the terminal.
    log_path = directory.parent / "browser.log"
    log = open(log_path, "ab")  # noqa: SIM115 - handed to the child, not used here
    try:
        # start_new_session puts Chrome in its own process group, so it is
        # not a child that dies with this script, and not in the terminal's
        # group where a Ctrl-C would reach it.
        subprocess.Popen(
            launch_args(executable, directory, port_number, headless),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    finally:
        log.close()

    url = cdp_url(port_number)
    deadline = time.monotonic() + LAUNCH_TIMEOUT
    while time.monotonic() < deadline:
        if is_listening(url):
            return url
        time.sleep(0.25)
    raise ChromeError(
        f"Chrome did not open its debugging port {port_number} within "
        f"{LAUNCH_TIMEOUT:.0f}s; see {log_path}"
    )


def ensure(directory: Path, port_number: Optional[int] = None, headless: bool = False) -> str:
    """Return a CDP URL for the Auto-Apply Chrome, launching it if needed.

    A Chrome from an earlier run, still open on a previously filled form, is
    reused rather than replaced: Chrome will not share a profile directory
    with a running instance anyway, and the human may still be reading that
    form.
    """
    port_number = port() if port_number is None else port_number
    url = cdp_url(port_number)
    if is_listening(url):
        return url
    return launch(directory, port_number, headless=headless)


# How long Chrome gets to quit after Browser.close before it is reported as
# still running. Saving session state on a big profile has been seen take two.
CLOSE_TIMEOUT = 10.0


def open_pages(port_number: Optional[int] = None) -> list[str]:
    """URLs of the tabs open on our Chrome; [] when it is not running."""
    url = cdp_url(port() if port_number is None else port_number)
    try:
        with urllib.request.urlopen(f"{url}/json", timeout=2) as response:
            pages = json.loads(response.read())
    except Exception:  # noqa: BLE001 - no browser, no tabs
        return []
    return [p.get("url", "") for p in pages if p.get("type") == "page"]


def close_tab(target_id: str, port_number: Optional[int] = None) -> bool:
    """Close one tab on our Chrome by target id, over the /json/close
    endpoint. The browser and every other tab stay. False when the browser
    is not up or the tab is already gone."""
    if not target_id:
        return False
    url = cdp_url(port() if port_number is None else port_number)
    try:
        with urllib.request.urlopen(f"{url}/json/close/{target_id}", timeout=2) as response:
            return response.status == 200
    except Exception:  # noqa: BLE001 - no browser, or no such tab
        return False


def close(port_number: Optional[int] = None) -> bool:
    """Quit the Auto-Apply Chrome. Returns whether one was running.

    Only the browser on our debugging port is touched, so the human's own
    Chrome, on its own profile and no port, is never closed. The request is
    CDP's Browser.close, the same thing as Quit from the menu: every window
    goes and the profile is saved cleanly, so the next launch has the logins.
    """
    port_number = port() if port_number is None else port_number
    url = cdp_url(port_number)
    if not is_listening(url):
        return False
    with urllib.request.urlopen(f"{url}/json/version", timeout=2) as response:
        ws_url = json.loads(response.read())["webSocketDebuggerUrl"]
    from websockets.sync.client import connect  # browser-use's dependency

    try:
        with connect(ws_url, max_size=None) as socket:
            socket.send(json.dumps({"id": 1, "method": "Browser.close"}))
            try:
                socket.recv(timeout=2)
            except Exception:  # noqa: BLE001 - Chrome may drop the socket first
                pass
    except Exception:  # noqa: BLE001 - a closed socket mid-quit is success
        pass
    deadline = time.monotonic() + CLOSE_TIMEOUT
    while time.monotonic() < deadline:
        if not is_listening(url, timeout=0.5):
            return True
        time.sleep(0.25)
    return True
