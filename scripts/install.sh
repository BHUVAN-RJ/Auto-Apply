#!/bin/bash
# Install (or repair) Autopilot on this Mac. Safe to run again: every step
# checks before it acts. Written to be run by Claude Code following
# INSTALL.md, and readable by a person who wants to know what it does.
#
# What it does, in order:
#   1. checks: Apple Silicon, macOS, Homebrew, Google Chrome
#   2. brew installs uv, tectonic (LaTeX), espeak-ng (the voice),
#      whisper-cpp (speech in)
#   3. uv creates .venv in this folder with the Python the app needs
#   4. makes the data folder (~/Library/Application Support/Autopilot)
#      with the templates in it, and a .env for the OpenRouter key
#   5. compiles the master resume once, if it is there, so the first job
#      does not wait for Tectonic's package download
#   6. builds ~/Applications/Autopilot.app
#   7. runs the invariant tests
#
# It never asks for the OpenRouter key: the person types it into the app.

set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
HOME_DIR="${AUTOPILOT_HOME:-$HOME/Library/Application Support/Autopilot}"
say() { printf '\n==> %s\n' "$*"; }
fail() { printf '\nINSTALL STOPPED: %s\n' "$*" >&2; exit 1; }

say "Checking this Mac"
[ "$(uname -s)" = "Darwin" ] || fail "Autopilot runs on macOS only."
[ "$(uname -m)" = "arm64" ] || fail "Autopilot supports Apple Silicon Macs only (this one is $(uname -m))."
command -v brew >/dev/null || fail "Homebrew is missing. Install it from https://brew.sh (one command), then run this again."
[ -d "/Applications/Google Chrome.app" ] || [ -n "${AUTOPILOT_BROWSER:-}" ] || \
  fail "Google Chrome is missing. Install it from https://www.google.com/chrome/, then run this again."

say "Installing tools with Homebrew (uv, tectonic, espeak-ng, whisper-cpp)"
for formula in uv tectonic espeak-ng whisper-cpp; do
  if brew list --formula "$formula" >/dev/null 2>&1; then echo "  $formula: already installed"
  else brew install "$formula"; fi
done

say "Creating the Python environment (.venv)"
cd "$REPO"
uv sync --python 3.13 --extra voice --extra dev

say "Making the data folder: $HOME_DIR"
mkdir -p "$HOME_DIR/base" "$HOME_DIR/data" "$HOME_DIR/applications" "$HOME_DIR/logs"
copy_template() {  # template, destination: only when the destination is missing
  [ -e "$2" ] || { cp "$1" "$2"; echo "  created $(basename "$2") from the template"; }
}
copy_template base/applicant.example.md "$HOME_DIR/base/applicant.example.md"
copy_template base/form.example.json "$HOME_DIR/base/form.example.json"
copy_template base/resume.example.tex "$HOME_DIR/base/resume.example.tex"
if [ ! -f "$HOME_DIR/.env" ]; then
  grep -v '^OPENROUTER_API_KEY=' .env.example | sed 's/^AUTOPILOT_AGENT=1/AUTOPILOT_AGENT=0/' > "$HOME_DIR/.env"
  echo "OPENROUTER_API_KEY=" >> "$HOME_DIR/.env"
  chmod 600 "$HOME_DIR/.env"
  echo "  created .env (the key is added from the app)"
fi

if [ -f "$HOME_DIR/base/resume.tex" ]; then
  if [ ! -f "$HOME_DIR/base/resume.pdf" ] || [ "$HOME_DIR/base/resume.tex" -nt "$HOME_DIR/base/resume.pdf" ]; then
    say "Compiling the master resume (the first compile downloads LaTeX packages; a minute or two)"
    AUTOPILOT_HOME="$HOME_DIR" .venv/bin/python -c "
from pathlib import Path
from tex import compile as c
import paths
out = c.compile_pdf(paths.BASE / 'resume.tex', paths.BASE / 'resume.pdf')
print('  compiled', out, c.page_count(out), 'page(s)')
"
  fi
else
  say "No master resume yet: $HOME_DIR/base/resume.tex (INSTALL.md, step 4)"
fi

say "Building ~/Applications/Autopilot.app"
"$REPO/scripts/make_app.sh"

say "Checking the invariants"
"$REPO/scripts/check.sh" --quick

say "Installed"
echo "  Open Autopilot from ~/Applications (or: scripts/autopilot)."
echo "  Your data: $HOME_DIR"
