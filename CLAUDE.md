# Auto-Apply — working notes for Claude

Semi-autonomous job application pipeline. Read [PLAN.md](PLAN.md) for the
design and [README.md](README.md) for what it does. This file is the part that
is not obvious from the code.

Repo: `~/Desktop/job-autopilot`, pushed to `github.com/BHUVAN-RJ/Auto-Apply`.

## The rule the whole project exists to protect

**The agent never submits an application, and never closes a job.** Both are
enforced in code, not in prompts, and both have been broken by accident once
already. Before changing anything in `browser/` or the status transitions, read
the Invariants section of PLAN.md.

Specifically:

- `browser/guard.py` refuses any click that reads as submit/apply/send/finish.
  `evaluate` and `send_keys` are removed from the agent's vocabulary entirely.
- The fill must leave the browser **window open** on the completed form. Never
  call `browser.kill()`, and never let browser-use launch Chrome: its watchdog
  kills whatever it launched on any stop, ignoring `keep_alive`. Chrome is
  started by `browser/chrome.py` (detached, port 9333) and browser-use only
  attaches by CDP URL. Tests assert `kill()` and `user_data_dir` are absent.
- The agent never answers a visa / sponsorship / work-authorisation / OPT
  question. `guard.check_protected` refuses click, input, and select_dropdown
  on any field whose label (or an ancestor's, for radios) reads that way.
  Jobright's autofill may set them; the agent leaves what it finds.
- The browser model never writes prose. Open questions go through the
  `answer_question` action to the tailor model (`tailor/answers.py`), which
  refuses protected questions too. Answers are shown on the review page.
- `upload_file` is wrapped: refused when the target has no file input of its
  own (browser-use would otherwise fall back to the nearest one on the page)
  and when that input reads as a cover letter. Greenhouse: `resume` vs
  `cover_letter`.
- The screen verdict (`reject` / `caution` / `ok`) is advice on the page and
  the review page. It never sets `skipped`, never blocks `/capture`, and a
  failed screen never stops the pipeline (`screen_error.txt` instead).
- A poor-fit verdict is a *recommendation*. It stops at checkpoint 1 for the
  human; it does not set `skipped`.
- `filled` requires the agent to have actually finished **and** a successful
  `upload_file` of the tailored resume in the action log. The model has
  claimed "resume attached" when its upload was refused; the claim is never
  trusted.
- **One fill per job.** `runner.launch("apply.py")` returns None while one
  is alive (`runner.fill_pid`, tracked Popen plus a `pgrep` fallback).
  `/fill` answers 409 `fill_running` and needs `force`, which the page sends
  only after "This will kill the current job and restart" is confirmed.
  Approve plus "Fill the form" once produced two agents on one tab.
- **Only the server closes the browser**, on `/submitted`, and only when no
  other job is approved / filling / filled. `browser/chrome.py: close()` is
  CDP `Browser.close` on port 9333, never the user's own Chrome. The agent
  still never closes anything.

## Layout

| Path | What |
|---|---|
| `pipeline.py` | queued job → scrape → tailor → compile → cover letter → archive → checkpoint 1. Started by `/capture` |
| `apply.py` | approved job → named copies of resume/letter → fill form → screenshot → stop (checkpoint 2). Started by approve |
| `server/` | FastAPI on 8787: queue, review API, `runner.py` launches the scripts. `/capture` starts `pipeline.py` itself |
| `tailor/rules.md` | the tailoring prompt, sent verbatim — edit this, not the Python |
| `tailor/cover_rules.md` | the cover letter prompt, same rule |
| `tailor/cover.py` | letter from the tailored resume; plain pdflatex template; failure is non-fatal |
| `tailor/answers.py` + `answer_rules.md` | free-form form questions, answered by the tailor model via the agent's `answer_question` action; archived to `answers.md` |
| `tailor/screen.py` + `screen_rules.md` | on-page auto-reject screen: posting vs `base/applicant.md` Facts, fixed category enum, verdict recomputed from flags in code |
| `server/screen.py` | `POST /screen`, URL-keyed cache in `data/screens.json`; the pipeline reuses it |
| `server/settings.py` | the "Use profile" switch, `data/settings.json`; scripts read it too |
| `tailor/profile.py` | what the models are told about the applicant. `context()` = profile.md + applicant facts + `base/stories/` when the switch is on, profile.md alone otherwise. `screen_facts()` falls back to facts derived from the resume, cached in `data/derived_facts.md` |
| `capture/content.js` | reads the page, calls `/screen`, paints the banner. Injected by match list and into any tab opened from jobright.ai |
| `browser/guard.py` | the never-submit deny-list |
| `browser/chrome.py` | launches and reuses the Chrome that browser-use attaches to |
| `tex/compile.py` | engine picked per document, not fixed |
| `archive/store.py` | immutable per-application folders |
| `review/index.html` | the whole UI, one file, no build step; holds the "Use profile" switch |
| `capture/background.js` | context menu, and follows tabs off Jobright / LinkedIn to inject the screen wherever Apply lands |
| `tools/sweep_failed.py` | moves `failed` application folders under `applications/failed/` and repoints queue rows; nothing deleted |

Config lives in `.env` (gitignored). `AUTOPILOT_RESUME_FILENAME` and
`AUTOPILOT_COVER_LETTER_FILENAME` there name the uploaded PDFs (spaces become
underscores); the archive keeps `resume.pdf` / `cover_letter.pdf` and a copy
under each name. `base/resume.tex` is the master resume and
is **gitignored** — the repo is public and the resume carries real contact
details. Same for `base/profile.md` and `base/applicant.md`
(`base/applicant.example.md` is the template; the `## Facts` section is what
the screen reads. Missing, or with the "Use profile" switch off, the screen
uses facts derived from the resume and the banner says so).

## Things that will bite you

Every one of these cost a debugging cycle. They are in PLAN.md in more detail.

- **BasicTeX is minimal.** A new template will be missing packages. The compile
  error parses the names and prints the `tlmgr` command. `fullpage` lives in
  `preprint`, not a package of its own.
- **Engine choice matters.** Resume templates using `\input{glyphtounicode}`
  need pdflatex; fontspec needs lualatex. Detection strips comments first,
  because a commented-out `\setmainfont` was enough to pick wrong.
- **Reasoning tokens come out of the answer budget.** A thinking model can
  spend the whole allowance and return a null `content`. Effort is capped.
- **The browser model must accept images and be reliable at tool schemas.**
  A text-only model 404s on every screenshot; `glm-5v-turbo` accepts images but
  mis-formats actions and fails validation on every step. Both fail totally and
  quietly.
- **browser-use blinds DeepSeek.** `deepseek-v4.1-flash` accepts images on
  OpenRouter, but browser-use logs "DeepSeek models do not support
  use_vision=True yet" and turns screenshots off by model name. The fill
  then clicks autofill and stalls. Browser model stays on Gemini; DeepSeek
  is fine for the text-only screen.
- **Chrome will not share a profile directory** with a running instance — it
  silently falls back to a throwaway and the login is lost. The fill loop owns
  `~/Library/Application Support/job-autopilot/chrome`.
- **A stale uvicorn holds port 8787** and serves old code. If behaviour makes
  no sense, `pgrep -fl uvicorn` first. Scripts it launches run current code
  regardless, so "the pipeline works but the page is wrong" means this.
- **The model covers for a refused action.** It reported the resume as
  attached after every upload was refused. Read outcomes from the action log
  (`resume_uploaded`), never from the done text.
- **Module-level `Path` defaults freeze at import.** `def f(path=APPLICANT)`
  ignores a monkeypatched `APPLICANT`, so the test hit the real resume and
  the real model. Resolve paths inside the function (`path or APPLICANT`).
  Bitten twice in one session.
- **Reordering is legal, so nothing may compare bullets by position.**
  `tailor.pair_bullets` matches by similarity; use it, not `zip`.
- **A rerun must clear `error` on the queue row**, or the page shows the old
  failure over a good result.
- **The folder is named after the fetch, not before it.** The extension
  often captures no company (Jobright link landing on Ashby), so allocating
  the folder before `fetch.fetch` made every first run `unknown-company_...`.
  `pipeline.allocate` runs after the fetch; a failed fetch still gets a
  folder so its error has somewhere to land.
- **A form question is not a requirement.** The screen's first real run
  flagged "Will you now or in the future require sponsorship?" as a visa
  flag. The rules say bare questions are not flags; if it recurs, that is
  the section of `screen_rules.md` to sharpen.
- **Chrome lives on port 9333**, detached, reused across runs. If a fill
  attaches to the wrong thing, `curl 127.0.0.1:9333/json` shows its tabs.
- **Extra tabs mean extra fills.** Each `apply.py` opens its own tab. Four
  tabs on one job meant four runs; `pgrep -fl apply.py` before anything else.
- **`.env` has `AUTOPILOT_AUTOFILL=1`, so a test that approves a job launches
  the real `apply.py`** unless the fixture forces it off. `test_review.py`
  does; a stray `apply.py 3fbd` from a test run once sat in the process list.
- **The preamble check is byte-exact** (`_check_frozen_sections`; only
  whitespace runs are collapsed). The model burned two of four attempts on a
  commented-out font line and a dropped space in a macro. `rules.md` now says
  nothing above `\begin{document}` changes, not even a comment.

## Testing

`pytest`, 320 tests, no network and no browser. The suite stubs the model, the
browser, and lualatex, so **passing tests do not mean it works** — every real
bug so far survived a green suite and appeared on the first real run. Run
something end to end before claiming a fix.

## What the review page shows

Terse on purpose. The rationale is asked for in caveman style (see the last
section of `tailor/rules.md`; style after github.com/juliusbrussee/caveman),
the browser's done text is three fixed lines, fill errors are deduplicated
and cut to one line each, and only the latest fill's notes are shown. If the
page reads like prose again, one of those slipped.

## Personal settings, all in .env

`AUTOPILOT_RESUME_FILENAME`, `AUTOPILOT_COVER_LETTER_FILENAME`,
`AUTOPILOT_LOCATION`, `AUTOPILOT_AUTOFILL`. Never hardcode a name, a city, or
a phone number in the repo; it is public.

## Style

Full English in the repo: commits, comments, docs, this file. Conversation in
the terminal follows whatever mode is active.

Commits explain *why*, not what changed. The diff already says what changed.
