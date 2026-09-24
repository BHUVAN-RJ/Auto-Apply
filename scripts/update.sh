#!/bin/bash
# Bring a person's copy up to the latest release without losing their changes.
#
#   scripts/update.sh              merge the newest release tag (vX.Y.Z)
#   scripts/update.sh v0.3.0       merge that release
#   scripts/update.sh --continue   after resolving conflicts by hand (or by Claude)
#   scripts/update.sh --abort      give up on a merge in progress
#
# How a copy is laid out (UPDATING.md has the why):
#   - the person's own changes to the code are commits on the branch `mine`,
#     each one logged in LOCAL_CHANGES.md;
#   - their data and their prompt edits are not in the clone at all
#     ($AUTOPILOT_HOME), so an update never touches them; prompt edits are
#     three-way merged by the app itself on its next start;
#   - an update is a merge of a release tag into `mine`. Their commits stay,
#     the release's commits are added, and git remembers every conflict
#     resolution (rerere) so the same one is never asked twice.
#
# Files that hold the invariants take the release's version on a conflict,
# always: a local change is not allowed to weaken never-submit or the visa
# guard. Everything else is resolved for the person's intent, with
# LOCAL_CHANGES.md saying what that intent was.

set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

PROTECTED=(browser/guard.py tests/test_invariants.py tests/test_guard.py scripts/check.sh)

say() { printf '\n==> %s\n' "$*"; }
fail() { printf '\nUPDATE STOPPED: %s\n' "$1" >&2; exit "${2:-1}"; }

git config rerere.enabled true
git config rerere.autoupdate true

merging() { [ -f "$(git rev-parse --git-dir)/MERGE_HEAD" ]; }

conflicts() { git diff --name-only --diff-filter=U; }

resolve_protected() {
  local file
  for file in "${PROTECTED[@]}"; do
    if conflicts | grep -qx "$file"; then
      git checkout --theirs -- "$file" && git add -- "$file"
      echo "  $file: took the release's version (it holds an invariant)"
    fi
  done
}

finish() {
  say "Updating the Python environment"
  uv sync --python 3.13 --extra voice --extra dev || fail "uv sync failed"
  say "Checking the invariants and the suite"
  if ! scripts/check.sh; then
    local backup; backup="$(cat "$(git rev-parse --git-dir)/autopilot-backup" 2>/dev/null)"
    fail "the checks failed after the update. Fix the failure, or go back with:
    git reset --hard ${backup:-<the backup/ tag>}
(that discards the update and nothing else: your data is outside this folder)"
  fi
  if [ -f "${AUTOPILOT_HOME:-$HOME/Library/Application Support/Autopilot}/run/server.pid" ]; then
    say "Restarting Autopilot (the first start merges your prompt edits onto the new ones)"
    scripts/autopilot restart
  fi
  say "Updated to $(git describe --tags --match 'v*' --always)"
}

case "${1:-}" in
  --abort)
    merging && git merge --abort && echo "merge abandoned; nothing changed" ; exit 0 ;;
  --continue)
    merging || { finish; exit 0; }
    resolve_protected
    [ -z "$(conflicts)" ] || fail "still conflicted:
$(conflicts)
Resolve them (UPDATING.md, step 3), git add each, then run this again." 3
    git commit --no-edit || fail "the merge commit failed"
    finish; exit 0 ;;
esac

[ "$(git rev-parse --abbrev-ref HEAD)" = "mine" ] || \
  fail "updates are merged into the branch 'mine', and this is '$(git rev-parse --abbrev-ref HEAD)'. See UPDATING.md."
merging && fail "a merge is already in progress: finish it with --continue or drop it with --abort."
[ -z "$(git status --porcelain --untracked-files=no)" ] || \
  fail "there are uncommitted changes. Commit them to 'mine' first (and log them in LOCAL_CHANGES.md)."

say "Fetching releases"
git fetch --tags origin || fail "could not reach origin"
TARGET="${1:-$(git tag --list 'v*' --sort=-v:refname | head -1)}"
[ -n "$TARGET" ] || fail "no release tags on origin yet"
git rev-parse -q --verify "$TARGET^{commit}" >/dev/null || fail "no release called $TARGET"
if git merge-base --is-ancestor "$TARGET" HEAD; then echo "  already up to date with $TARGET"; exit 0; fi

BACKUP="backup/$(date +%Y%m%d-%H%M%S)"
git tag "$BACKUP"
echo "$BACKUP" > "$(git rev-parse --git-dir)/autopilot-backup"
echo "  your copy as it was is tagged $BACKUP"

say "Merging $TARGET into mine"
if git merge --no-ff --no-edit -m "update to $TARGET" "$TARGET"; then
  finish; exit 0
fi

resolve_protected
if [ -z "$(conflicts)" ]; then
  git commit --no-edit && { finish; exit 0; }
fi
fail "these files changed both in your copy and in $TARGET:
$(conflicts)
Resolve each so that your change and the release's change are both kept
(UPDATING.md, step 3; LOCAL_CHANGES.md says what your change was for),
git add it, then run: scripts/update.sh --continue
To give up instead: scripts/update.sh --abort" 3
