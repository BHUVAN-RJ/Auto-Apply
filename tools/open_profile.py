#!/usr/bin/env python3
"""Open Auto-Apply's browser profile so you can set it up once.

Run this, then inside the window that opens:

  1. Sign in to Jobright (and any ATS you keep an account with).
  2. Install the Jobright extension.
  3. Load the capture extension from this repo's capture/ directory.

All of it persists, because this is a real profile directory that the fill
loop reuses on every run. Close the window when you are done.

    python tools/open_profile.py            # launch the profile
    python tools/open_profile.py --attach   # print how to use your own browser
"""

from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from browser import chrome  # noqa: E402
from browser.chrome import BRAVE, CHROME, find_browser  # noqa: E402
from browser.fill import profile_dir  # noqa: E402


def attach_instructions() -> str:
    return f"""To use a browser you already have open, with its existing logins
and extensions, start it with a debugging port:

  Chrome:
    "{CHROME}" --remote-debugging-port=9222

  Brave:
    "{BRAVE}" --remote-debugging-port=9222

Quit the browser completely first — the flag only applies to a fresh launch.
Then point Auto-Apply at it:

  export AUTOPILOT_CDP_URL=http://127.0.0.1:9222

Leave that browser running while you use apply.py.
"""


def main(argv: list[str]) -> int:
    if "--attach" in argv:
        print(attach_instructions())
        return 0

    browser = find_browser()
    if browser is None:
        print("No Chrome or Brave found in /Applications.", file=sys.stderr)
        return 1

    directory = profile_dir()
    print(f"Opening {Path(browser).name} with profile:\n  {directory}\n")
    print("Sign in to Jobright and install its extension. Leave the window open")
    print("or close it; apply.py starts the same browser again either way.\n")

    # The same launcher the fill loop uses, so this is the exact browser a
    # fill will attach to, on the same port. If one is already up it is
    # reused and just gets a new tab.
    url = chrome.ensure(directory)
    request = urllib.request.Request(f"{url}/json/new?https://jobright.ai", method="PUT")
    with urllib.request.urlopen(request, timeout=5):
        pass
    print(f"Browser is up at {url}. Output goes to {directory.parent / 'browser.log'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
