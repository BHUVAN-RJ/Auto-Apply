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

# Never the person's own data: the tests write into temporary folders, and
# with AUTOPILOT_HOME unset they would read the clone's (empty) base/.
unset AUTOPILOT_HOME

INVARIANTS=(tests/test_invariants.py tests/test_guard.py tests/test_forms.py
            tests/test_screen.py tests/test_tailor.py tests/test_prompts.py)

echo "== invariants"
"$PY" -m pytest -q "${INVARIANTS[@]}" || { echo "INVARIANTS FAILED: undo the change that broke them." >&2; exit 1; }

[ "${1:-}" = "--quick" ] && exit 0

echo "== whole suite"
# test_fill.py needs browser-use, which the app no longer installs.
"$PY" -m pytest -q --ignore=tests/test_fill.py
