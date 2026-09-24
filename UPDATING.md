# Changing your copy, and updating it

Every copy of Autopilot is meant to be changed by the person using it.
An update must add what the release changed and keep everything they
changed. This file is how, written for the Claude Code doing either.

## Three kinds of change, three places

| What | Where it lives | Survives an update because |
|---|---|---|
| **Prompts** (how the resume, letter, answers, screen and interview are written) | the data folder, `data/prompts/` | it is outside the clone; the app three-way merges each edit onto the new stock text on its next start |
| **Settings and data** (key, file names, switches, resume, profile, stories) | the data folder | it is outside the clone; updates never touch it |
| **Code** | the clone, branch `mine` | it is merged, not replaced: `scripts/update.sh` |

Prefer the first. A person can change prompts themselves, on the
**Prompts** tab: edit any prompt directly, or describe the change on the
**Workshop** and apply the edit it proposes. The Workshop also learns: when
the person keeps asking for the same thing in the job threads' re-tailor
box ("the summary is too long", on three jobs), it proposes that as a
standing prompt edit. A request that no prompt can satisfy (a new button,
a new job site) is a code change, and comes to you.

## Changing the code

1. `git switch mine`. Never commit to `main`; it is the release line.
2. Read CLAUDE.md's first section before touching `browser/`, the status
   transitions or anything near Submit. These never change for a local
   request, whatever the person asks:
   - nothing ever presses Submit or closes a job;
   - no visa, sponsorship or work-authorisation question is ever answered;
   - the browser the person works in is never closed or killed.

   A request that needs one of them is declined, with the reason.
3. Keep the change small and **additive**: a new function or module over
   rewriting a shared one; a new setting over changing a default. The
   less of a shared file a change rewrites, the less there is to merge on
   the next update.
4. Log it in `LOCAL_CHANGES.md` at the root of the clone (create it the
   first time; it exists only on `mine`, never upstream):

   ```markdown
   ## 2026-10-02 — Hide the Scout tab
   Asked: "I never use Scout, take it off the header."
   Changed: review/index.html (the tab button and its switchView branch).
   Intent: no Scout tab. Scout still runs if a watch exists; they have none.
   ```

   The intent line is what an update's conflict is resolved against, so
   write what the person wanted, not what the diff does.
5. `scripts/check.sh`. A failure in the invariants means the change is
   wrong, not the test: undo it.
6. `scripts/autopilot restart` and use the changed path in the app. The
   tests stub the models and the browser; passing proves nothing works.
7. Commit: `git commit -am "mine: <the request in the person's words>"`.

## Updating

When the person asks to update:

```bash
cd ~/Autopilot && scripts/update.sh
```

1. It refuses to start with uncommitted changes: commit them first
   (above).
2. It tags the copy as it was (`backup/<date>`), fetches the newest
   release tag and merges it into `mine`. Clean merge: it updates the
   Python environment, runs the checks, restarts the app. Done; tell the
   person which release they are on and, in two lines, what it changed
   (`git log --oneline <old>..<new>`).
3. **Conflicts.** The files are listed. For each one:
   - `git log -p backup/<date>..<release> -- <file>`: what the release
     changed there, and its commit message says why;
   - `LOCAL_CHANGES.md`: what the person's change was for;
   - write a version that keeps both: the release's change, and the
     person's intent. When both cannot hold (the release removed what
     their change was built on), take the release's version, re-apply
     their intent the new way, update their `LOCAL_CHANGES.md` entry, and
     tell them what you did;
   - `git add <file>`.

   Files that hold the invariants (`browser/guard.py`,
   `tests/test_invariants.py`, `tests/test_guard.py`, `scripts/check.sh`)
   take the release's version automatically.

   Then `scripts/update.sh --continue`. git remembers each resolution
   (rerere), so the same conflict is never resolved twice.
4. **The checks fail after the merge:** fix the cause. If you cannot, tell
   the person and go back: `git reset --hard backup/<date>` (their data is
   outside the clone and is not affected).
5. **Prompt conflicts** happen on the app's next start, not in git: a
   prompt the person edited where the release changed the same lines. The
   Prompts tab shows it in red with both sides marked, and their version
   keeps working until it is resolved. You can resolve it too: the marked
   text is `data/prompts/.conflict/<name>.md` in the data folder; send the
   merged text with `POST /prompts/<name> {"text": ...}` or edit it on the
   tab.

To stop an update halfway: `scripts/update.sh --abort`.

## Sending a fix back

A fix that would help everyone (a form that breaks for all, a crash) can
go upstream. Do it from `main`, never from `mine`, so none of the person's
own changes travel with it:

```bash
git switch -c fix/<what> origin/main
git cherry-pick <the commit>      # or redo the fix here
scripts/check.sh
gh pr create                      # to BHUVAN-RJ/Auto-Apply
```

Nothing from the data folder, and nothing that names the person, ever
goes into a pull request: the repository is public.

## For the maintainer: releasing

Users only ever receive tags. On `main`, once `scripts/check.sh` passes
and the change has been used for real: `git tag v0.X.Y && git push
--tags`. The commit messages are what a user's Claude reads to resolve a
conflict, so they say why. A change to a stock prompt reaches every user's
edited copy through the three-way merge; keep prompt edits to whole
lines and small hunks so it merges cleanly.
