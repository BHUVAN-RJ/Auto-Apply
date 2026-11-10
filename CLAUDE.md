# Auto-Apply — working notes for Claude

Semi-autonomous job application pipeline. [PLAN.md](PLAN.md) is the design,
[README.md](README.md) is what it does, and [HISTORY.md](HISTORY.md) holds the
reasoning, the measurements and the dated history behind every line here.
HISTORY.md is not loaded into a session, so **grep it for a module or a
behaviour before changing that behaviour**, and log new work at its end, one
line with the date. This file is only what must be known in every session.

Repo: `~/Desktop/job-autopilot`, pushed to `github.com/BHUVAN-RJ/Auto-Apply`.

## If this is someone's installed copy

Autopilot is shared by pointing a person's own Claude Code at this repository.
If you are that Claude, on a clone whose branch is `mine`: read **INSTALL.md**
for the install and the first run, and **UPDATING.md** before changing
anything, including which requests are a prompt change, which are a screening
rule, and which are code. Follow them; they are how a change survives the next
release. Their data lives in `~/Library/Application Support/Autopilot`
(`AUTOPILOT_HOME`), never in the clone — never copy it in, commit it or push
it, because the repository is public. The rule below holds on every copy,
whoever asks.

## The rule the whole project exists to protect

**The agent never submits an application, and never closes a job.** Both are
enforced in code, not in prompts, and both have been broken by accident once
already. Before changing anything in `browser/` or the status transitions, read
the Invariants section of PLAN.md. The promises are tested by
`tests/test_invariants.py` and registered in `tests/test_guardrails.py`.

- Only a person presses Submit: the form's own tab, or **Submit it** on the
  review page (`browser/press_submit.py`, `POST /review/{id}/submit`). No model
  reaches that endpoint, and `apply.py` and `browser/` may not import the
  presser. A press is not a submission: a job is SUBMITTED only once the page
  is a confirmation, with the form gone and `watch.CONFIRMED` in its text.
- `browser/guard.py` refuses any click that reads as submit, apply, send or
  finish. `evaluate` and `send_keys` are not in the agent's vocabulary.
- The browser stays open on the finished form. Never call `browser.kill()` and
  never let browser-use launch Chrome: `browser/chrome.py` starts it, detached
  on 9333, and browser-use attaches by CDP URL. `/submitted` closes only that
  job's form tab.
- Nobody automated answers a visa, sponsorship, work-authorisation or OPT
  question: `guard.check_protected` refuses the agent, the answer model refuses
  them, and those keys are not in `forms.profile.KEYS`. Changing that is a
  decision, not a bug fix.
- The browser model never writes prose. Open questions go to the tailor model
  (`tailor/answers.py`) and are shown on the review page.
- `upload_file` is refused when the target has no file input of its own or when
  that input reads as a cover letter.
- `filled` needs a successful resume upload in the action log. Never trust the
  model's claim.
- One fill per job and one tab at a time, through `runner.start_fill`.
- The screen verdict is advice. It never sets `skipped`, never blocks
  `/capture`, and a failed screen never stops the pipeline. Nothing acts on a
  verdict alone: **Add to autopilot** and **Start now** are the person's
  presses (2026-10-30), and the automatic path no longer presses Apply.
- Match tabs by form, not path (`autofill.same_page`), or one job's resume lands
  on another job's form.

## Layout

Only the paths whose name does not tell you what they are. Everything else is
named after what it does. Grep HISTORY.md for a path to get its reasoning.

- `paths.py`: `ROOT` is the clone, `HOME` is `AUTOPILOT_HOME`; data hangs off `HOME`.
- `pipeline.py`: queued job to scrape, tailor, compile, cover letter, archive, checkpoint 1.
- `apply.py`: approved job to named documents, form fill, screenshot, stop at checkpoint 2.
- `mailing.py`: shared SMTP for Scout digests and approved outreach.
- `server/`: FastAPI on 8787; `runner.py` launches the scripts.
- `server/thread.py`: one conversation per job, in `data/threads/`.
- `server/report.py`, `.github/ISSUE_TEMPLATE/`: prefilled GitHub issue, never sent by the app.
- `server/screen.py`: `POST /screen` with a URL-keyed cache; confirmation pages skip the screen.
- `server/postings.py`: posting text the browser saw, for when the fetch gets a shell.
- `server/watch.py`: thread reading filled forms over CDP every 4 s (`AUTOPILOT_WATCH=0` off).
- `server/followqueue.py`: every submitted application through the checking phase, one at a time, no cap.
- `tailor/rules.md`, `cover_rules.md`: tailoring and letter prompts, sent verbatim. Edit these, not the Python.
- `tailor/prompts.py`: every system prompt through one door (`CATALOG`, `text(name)`).
- `tailor/layout.py`: line budget measured off `base/resume.pdf`. `tailor/structure.py` reads any person's LaTeX layout.
- `tailor/byhand.py`: "Use Opus" by pasting, with no API bill.
- `tailor/screening.py`: screen rules in code. `tailor/screen.py`: screen and its two policies. No model.
- `tailor/claudecode.py`: Claude Code subscription calls, with no CLAUDE.md on any call.
- `outreach/follow.py`: the phase a submission starts; `company_for` reads the employer off a row that names none.
- `outreach/people.py`, `apollo.py`, `jobright.py`: the free, the paid and the guessed rungs; only Apollo returns a verified address.
- `outreach/gmailthing.py`, `linkedinthing.py`: their own tools in `~/outreach`; Gmail is signed in as the address they write from, LinkedIn owns the invite cap.
- `tracking/`: every tab in the Chrome we own, watched until it closes; `policy.py` decides what may be recorded.
- `capture/content.js`: reads the page, calls `/screen`, paints the banner.
- `browser/inject.py`: the same capture without an extension, over CDP on 9333.
- `browser/forms/`: no-model fill, one adapter per system. `table.json`, `table.py`, `steps.py` hold a system's form as data.
- `browser/ats.py`, `ats_rules.md`: per-system notes for the browser model.
- `browser/workday.py`, `browser/linkedin_apply.py`: page walks that stop at the review page.
- `archive/store.py`: immutable per-application folders.
- `review/index.html`: the whole UI, one file, no build step.
- `identity/`: "have I applied to this?", deterministic, no model.
- `boardscan/`, `server_boardscan.py`: jobs from every company's board, its own server on 8788.
- `tools/sweep_failed.py`: moves failed folders, deletes nothing.
- `tests/dom/`, `tests/test_dom.py`: the only tests using a browser.

Config lives in `.env` (gitignored, documented in `.env.example`).
`base/resume.tex`, `base/profile.md` and `base/applicant.md` are gitignored
because the repo is public; `base/applicant.example.md` is the template.
Never hardcode a name, a city, an email address or a phone number anywhere in
the repo; a resume's filename is a person's name.

## Things that will bite you

Each one cost a debugging cycle. Full accounts are in HISTORY.md, which also
holds the per-site DOM lore — what Greenhouse, Lever, Jobright and react-select
each need — and the voice internals. Grep it before touching one of those.

**Processes and ports**
- **Two servers, two data folders, and the queue reads empty.** Before concluding anything was deleted: `pgrep -fl uvicorn`, compare both data folders, and read `home` from `GET /setup`.
- **A stale uvicorn holds 8787** and serves old code, including the page. A 404 from a button means this. Re-tailor runs in the server's process, so restart after editing `tailor/`.
- **Something else can hold 8787**, such as `caveman-proxy`. `lsof -nP -iTCP:8787 -sTCP:LISTEN` names it. `AUTOPILOT_SERVER_URL` moves the server and the injector together; a mismatch splits state.
- **The injector runs the code it was started with.** Restart `browser.inject` after editing it or `browser/autofill.py`. If banners vanish, `pgrep -fl browser.inject`.
- **Chrome keeps the extension's old `content.js`** until reloaded at `chrome://extensions`. Client guards are a convenience; enforce on the server.
- **Chrome lives on 9333, and it is the only one.** `curl 127.0.0.1:9333/json` shows its tabs. A profile directory is never shared with a running Chrome, but a *running* Chrome is: the Gmail and LinkedIn tools in `~/outreach` attach to this one over CDP rather than starting their own (2026-11-10), so each needs a one-time sign-in in it, nothing may close a tab it did not open, and the tracker holds LinkedIn off limits except under `/jobs`.
- **Extra tabs mean extra fills.** `pgrep -fl apply.py` first.

**Models**
- Reasoning tokens come out of the answer budget, so effort is capped.
- The browser model must accept images and handle tool schemas. It stays on Gemini: browser-use turns vision off for DeepSeek by name, and `glm-5v-turbo` fails validation.
- A model covers for a refused action and ignores "stop". Read the action log and enforce in code (`MOVE_ON`).
- The interview model sometimes skips its `COVERED:` header. Never assume it.
- A slow provider looks like a dead voice. `lsof -nP -p <uvicorn pid> | grep :443` tells stalled from broken.

**Tailoring and LaTeX**
- Every person keeps their own LaTeX design. Fix an unknown heading in `ROLES`, never by moving a resume into a template.
- BasicTeX lacks packages; the compile error prints the `tlmgr` command. `\input{glyphtounicode}` needs pdflatex and fontspec needs lualatex.
- Nothing may compare bullets by position. Use `tailor.pair_bullets`.
- A tailored resume links only what the master links or a story's `Link:` line says.
- A preamble change is spliced back by `restore_preamble` and recorded as a warning.
- A re-tailor can hide behind a cached PDF, hence `?v=<folder>` and `no-store`.
- A rerun must clear `error` on the queue row. Folders are named after the fetch, not before it.

**Screening**
- A form question or a candidate's answer is not an employer requirement (`is_question`, `is_answer`). A new false positive goes in the rule's `unless`, with a test using the posting's words.
- `enforce_location_policy` and `enforce_timeline_policy` are code invariants and run after the rules. Update their regression tests when changing either.

**Pages and forms**
- A filled form's tab must never be screened again. A snapshot pinged on submit can land after the navigation, so check the host and at least 3 fields.
- Never fetch from injected `content.js`; a page's service worker can hang it, so the CDP binding does the HTTP. Injection at `load` precedes client render, so wait for text. `window.open` from an evaluated script may be swallowed; the injector opens tabs.
- A generic label pattern matches words inside questions: textareas and labels over 60 characters are answered only from `answers`.

**Tests**
- `.env` has `AUTOPILOT_AUTOFILL=1`, so a test approving a job launches the real `apply.py` unless forced off.
- Module-level `Path` defaults freeze at import. Resolve inside the function.
- `tailor.llm` is one module, so stubbing it through `interview` stubs the scan too.
- Stub `window.alert` in a CDP smoke run, and never point a fake mic at the live server.

**Outside services**
- `POST /settings` changes only the keys sent.
- GitHub's `author=` misses commits whose email is unlinked; `409` on `/commits` is an empty repository.
- Google's careers JSON is gone. `scout.providers._google_rows` reads `ds:1` and raises when it is missing.
- Gmail SMTP takes an App Password only.

## Testing

`scripts/check.sh` runs after any change; `--quick` runs the invariants. A
bare `pytest` aborts the whole process rather than failing a test, inside
browser-use's display detection (KNOWN_ISSUES.md); run the focused files plus
`pytest --ignore=tests/test_fill.py`, which is what `check.sh` does. The
suite stubs the model, browser, lualatex and speech binaries, so **passing
tests do not mean it works**: every real bug so far survived a green suite.
Exercise the changed path live. `tests/test_dom.py` is the exception, running
real scripts against real markup in its own headless Chrome, and `check.sh`
says so out loud when it skips.

**A test that reads a script's source cannot tell a rewrite from a regression.**
Such a test is legitimate only where the text is the promise.

**Anyone may add a test; only the guardian removes one.** Deleting, renaming
away, skipping, xfailing or loosening a test goes through
`.claude/agents/test-guardian.md`. A failing test is a fact about the code.

## Known issues and open work

[KNOWN_ISSUES.md](KNOWN_ISSUES.md) is the live list of bugs; an entry leaves it
when the fix is logged in HISTORY.md with a date. Open work and what each
unfinished phase still lacks are in the Status sections of PLAN.md. Older ideas
may already be done, so check before building:
`grep -nE "Next if asked|not built|Raised" HISTORY.md`. Each of them is the
person's call, and the invariants come first if it changes the flow.

## What the review page shows

Terse on purpose. The rationale is asked for in caveman style (see the last
section of `tailor/rules.md`; style after github.com/juliusbrussee/caveman),
the browser's done text is three fixed lines, fill errors are deduplicated
and cut to one line each, and only the latest fill's notes are shown. If the
page reads like prose again, one of those slipped.

## Style

Full English in the repo: commits, comments, docs, this file. Conversation in
the terminal follows whatever mode is active.

Commits explain *why*, not what changed. The diff already says what changed.

## The one-click thesis

Everything after Phase 6 is built toward a single installable app (see
PLAN.md, "The one-click thesis"). Before proposing a dependency, a model
runtime, a system install, or a hardcoded path, check it against that
section and raise it in the conversation if it does not fit. Torch, a
separate TeX installer, and "run this in the terminal first" all fail it.
