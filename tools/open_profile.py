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

import subprocess
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from browser.fill import profile_dir  # noqa: E402

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
BRAVE = "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"


def find_browser() -> Optional[str]:
    for candidate in (CHROME, BRAVE):
        if Path(candidate).exists():
            return candidate
    return None


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
    directory.mkdir(parents=True, exist_ok=True)
    print(f"Opening {Path(browser).name} with profile:\n  {directory}\n")
    print("Sign in to Jobright and install its extension, then close the window.")
    print("Everything you do here persists for every later fill.\n")

    subprocess.run([
        browser,
        f"--user-data-dir={directory}",
        "--no-first-run",
        "--no-default-browser-check",
        "https://jobright.ai",
    ])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
