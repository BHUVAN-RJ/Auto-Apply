#!/bin/bash
# Is this copy still sound? Run after an install, an update, or any change.
#
#   scripts/check.sh           the invariants, then the whole suite
#   scripts/check.sh --quick   the invariants and the files that guard them
#
# The suite stubs every model and the browser, so passing does not prove a
# change works; it proves the change broke none of the rules. Exercise the
# changed path in the app as well.

set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
PY="$REPO/.venv/bin/python"

# Never the person's own data: tests/conftest.py gives the suite a data
# folder of its own, whatever AUTOPILOT_HOME says.

INVARIANTS=(tests/test_invariants.py tests/test_guard.py tests/test_forms.py
            tests/test_screen.py tests/test_tailor.py tests/test_prompts.py)

echo "== invariants"
"$PY" -m pytest -q -p no:warnings "${INVARIANTS[@]}" || { echo "INVARIANTS FAILED: undo the change that broke them." >&2; exit 1; }

[ "${1:-}" = "--quick" ] && exit 0

echo "== whole suite"
# test_fill.py needs browser-use, which the app no longer installs.
# test_dom.py runs below, under a heading of its own, so its skip is seen.
"$PY" -m pytest -q -p no:warnings --ignore=tests/test_fill.py --ignore=tests/test_dom.py || exit 1

# tests/test_dom.py runs the form scripts against real markup in a real
# browser, and skips when there is no Chromium. That skip is the only place
# the resume slots, the remove step and the logo are checked against a page,
# so it is said out loud rather than counted as a pass: a silent skip here
# is how a green suite stops meaning anything.
echo "== the browser tests"
if "$PY" -c 'import sys; sys.path.insert(0, "."); from browser import chrome; sys.exit(0 if chrome.find_browser() else 1)'; then
  "$PY" -m pytest -q -p no:warnings tests/test_dom.py || exit 1
else
  echo "SKIPPED: no Chrome. The Ashby, Workday and Greenhouse form scripts" >&2
  echo "         were NOT checked against a page. Install Chrome and run again." >&2
fi
