# job-autopilot

A semi-autonomous job application assistant. It rebuilds the manual Jobright
workflow as an agent pipeline, with two mandatory human checkpoints and a
hard guarantee that the agent never submits an application.

## The workflow this replaces

Today, manually:

1. Open Jobright, click through a recommendation to the job description or
   application page.
2. Copy the posting into Claude along with the candidate profile, and get
   tailoring suggestions back.
3. Paste the resume LaTeX in so Claude can produce the exact edits, compile
   it in Overleaf, download the PDF.
4. Click Apply. The Jobright browser extension autofills the form. Replace
   the resume it attached with the freshly tailored PDF, review the form by
   eye, and submit.

The agent performs steps 1 through 4 up to (but never including) the submit
click, pausing twice for human approval.

## Design decisions

| Decision | Choice | Reason |
|---|---|---|
| Location | `~/Desktop/job-autopilot`, sibling to the `ai-job-search` clone | Personal data stays out of a public fork; no upstream merge pain |
| Language | Python 3.10+ | `browser-use` is Python-native; queue, archive, diff, and LaTeX compile are all trivial in it |
| Browser control | `browser-use` driving the real Chrome profile | The Jobright extension must be loaded and logged in; a fresh automation profile would not have it |
| Autofill | Reuse the Jobright extension's own autofill | Reimplementing per-ATS field mapping is the single largest time sink and buys nothing |
| Models | OpenRouter | Free model choice; cheap models are adequate for DOM-level work, stronger ones for tailoring |
| Typesetting | Local BasicTeX, engine chosen per document | Removes the Overleaf round trip; deterministic, scriptable, offline |
| Browser model | Must accept images | `browser-use` sends a screenshot to the model every step; a text-only model 404s on all of them |
| Checkpoint UI | Local web page on `localhost:8787` | This is the surface the eventual glasses flow renders, so it is not throwaway work |
| ATS scope (v1) | Whatever Jobright autofill already handles | Autofill reuse means no per-ATS code in v1 |

### The one-click thesis

The end state is an installable app: one download, one install, plus the
browser extension. No terminal, no `pip`, no TeX installer, no model
download the user has to run by hand. Every design choice from Phase 7 on
is checked against this, and anything that does not fit is raised at the
time it is proposed, not after it is built. Concretely:

- **No heavy Python ML dependencies in the server.** Voice runs through
  subprocess binaries (whisper.cpp for speech to text, Piper or Kokoro
  ONNX for text to speech). Anything that pulls in torch is out; torch
  alone is larger than the rest of the app.
- **Model files are downloaded on first run**, with a progress bar, into
  the app data directory. The installer stays small.
- **LaTeX has to ship with the app.** BasicTeX is a separate installer and
  its missing packages are already a known bite. Tectonic (single binary,
  packages fetched on demand, supports fontspec) is the planned third
  engine in `tex/compile.py`; it has to work before packaging starts.
- **All state lives under the app data directory or `base/`.** No
  hardcoded machine paths. Secrets (the OpenRouter key) move from `.env`
  to a first-run screen when packaging starts; until then `.env` stands.
- **Chrome stays the user's own.** The fill attaches over CDP. The capture
  needs no extension: `browser/inject.py` puts the script into our
  Chrome's tabs over the same port (Phase 10).
- **The shell is Electron or Tauri around the existing Python server.**
  The page on 8787 is the UI already; the shell adds a window, spawns the
  bundled Python, and nothing else.
- **macOS signing and notarisation are required** for a download to open
  without a Gatekeeper warning: Apple Developer Program, US$99 a year,
  configured once in the packager. Nested binaries (Python, whisper.cpp)
  must be signed too; that is the usual failure. Windows signing is
  optional and comes later.

## Architecture

```
capture/   MV3 Chrome extension. Context-menu "Add this job" on any page.
           Posts {url, title, source, added_at} to the local server. Its
           content script screens Jobright posting pages and every page
           Jobright's Apply lands on, paints the verdict, and queues a
           clean one by itself (content.js); background.js follows tabs
           off Jobright to wherever they go.

server/    FastAPI on localhost:8787. Owns queue.json. Serves the review UI.
           Holds approve/reject/revise state for both checkpoints.
           POST /screen with a per-URL cache (screen.py); the "Use
           profile" switch in settings.json (settings.py).

tailor/    Reads the posting plus the candidate profile, edits base/resume.tex,
           emits a unified diff and a written rationale per change. Also
           writes the cover letter (cover.py), answers the form's open
           questions on request (answers.py), and screens a posting for
           auto-rejects (screen.py). Each has its own rules file.
           profile.py decides what the models are told about the applicant.

compile/   lualatex wrapper. Deterministic output into the application folder.

browser/   browser-use fill loop. Attaches to a Chrome that chrome.py owns,
           opens the application URL, clicks Jobright autofill, replaces the
           resume, attaches the cover letter, fixes the location, hands open
           questions to the tailor model, screenshots, halts. guard.py is the
           deny-list: never submit, never touch a visa field.

review/    Localhost web page. Diff view, PDF preview, Approve / Reject / Chat.

archive/   Application folder writer plus index.csv.

scout/     Companies' own careers pages, watched on a schedule. detect()
           picks a provider from the URL, providers.py reads the public
           JSON (or server-rendered page) behind it, run.py filters titles
           by level, pre-screens in code, screens what is new, records
           hits, mails a digest. Never a browser. A notify watch (a
           referral is possible) queues nothing; every other watch queues
           a hit the screen does not reject, through the same steps as
           /capture. Phase 17, 17b.
```

## Flow

```
you click Apply on Jobright        -> landing page screened, OK / NOT OK bar
OK or CAUTION queues itself in 2 s ->  queue.json, and pipeline.py starts
(NOT OK waits for the button; any page still queues from the context menu)
(the injector presses Jobright's Autofill in the new tab meanwhile)
    fetch posting -> tailor -> compile -> cover letter
    CHECKPOINT 1   web page: rationale, diff, PDF, letter; approve, reject, or revise
                   passed by itself when the banner's auto-approve box was
                   left ticked and the screen did not reject
    approve starts apply.py (AUTOPILOT_AUTOFILL=1) in the tab Jobright filled
    code removes Jobright's resume, attaches the tailored one and the letter
    (AUTOPILOT_AGENT=1: the browser agent then fixes the location, answers
      open questions; =0, the default in .env: it stops here)
    screenshot, window left open
    CHECKPOINT 2   web page: screenshot + answers; you submit by hand
```

Both checkpoints block. Nothing proceeds past checkpoint 1 without a click,
unless auto-approve says so (the default since 2026-09-18): the box on the
banner, per job, decided during the three-second countdown, falling back to
the `auto_fill` switch. Then a clean screen and a clean tailor pass it by
themselves, because the clean verdict is what queued the job in the first
place, and the one decision the human makes is on the filled form. A poor
fit or a `reject` screen still waits at checkpoint 1. Checkpoint 2 always
blocks; nothing ever submits.

## Archive layout

```
applications/
  2026-09-10_stripe_backend-engineer_a3f1/
    job.json             url, source, title, company, scraped_at
    posting.md           full description snapshot
    screen.json          auto-reject verdict and flags; screen_error.txt if it failed
    resume.tex           tailored source
    resume.pdf           compiled artifact
    resume.diff          unified diff against base/resume.tex
    suggestions.md       rationale for each change, one terse line per change
    cover_letter.md/.tex/.pdf   the letter; cover_letter_error.txt if it failed
    <Name>_Resume.pdf, <Name>_cover_letter.pdf   the copies actually uploaded
    answers.md           every free-form answer typed into the form
    fill_notes.md        one block per fill attempt; the page shows the latest
    fill_screenshot.png  completed form, pre-submit
    status.json          filled | confirmed | submitted | skipped
base/
  resume.tex             master resume, gitignored
  profile.md             free-text profile, gitignored
  applicant.md           hard facts (Facts) and form details, gitignored
  stories/               one document per role or project, Phase 7, gitignored
data/
  queue.json             the queue
  screens.json           screen verdicts by URL
  settings.json          the "Use profile" switch
  derived_facts.md       screen facts inferred from the resume, keyed by its mtime
index.csv                one row per application
```

Folder name is date, company, role, and a four-character hash of the URL, so
it sorts chronologically and cannot collide. Nothing in `applications/` is
ever deleted or overwritten; re-tailoring the same job creates a `_v2` folder.
`applications/` is gitignored — it holds full employment history and every
job considered.

## Invariants

These are enforced in the action layer, not in a prompt. Prompt-level rules
leak; a deny-list in code does not.

- The agent may not click any element matching a submit-button pattern.
- The `evaluate` and `send_keys` actions are removed from the agent: arbitrary
  JavaScript would make the click guard decorative, and Enter submits a
  single-input form with no button click.
- The fill loop's terminal action is always a screenshot plus `status: filled`.
  `submitted` is reachable only from the review page, only on a job that is
  filled or still filling (the human's own submission wins over the agent).
- One fill per job. `runner.launch("apply.py")` refuses while one is alive,
  and `/fill` needs `force` — sent only after the human confirmed the kill.
- Nothing closes the browser. A submission closes that job's form tab and
  nothing else; the Jobright list, the next form and the logins stay. The
  agent never closes anything.
- Checkpoint 1 passes itself only for a clean tailor and a screen that is
  not `reject`, and only when the job's own auto-approve answer (the box on
  the banner at capture) or, failing one, the `auto_fill` switch says so.
  A poor fit or a `reject` screen always waits for the click. Nothing in
  that path can reach `filled`, let alone `submitted`.
- A fill with the agent switched off (`AUTOPILOT_AGENT=0`) touches no
  field. It removes the file on the resume slot (and on the cover letter
  slot when one is occupied) and sets ours; the one click it makes is a
  "Remove file" whose label has gone through `describes_submit`.
- A fill works in one tab per job, found by the whole URL minus visitor
  tags (`autofill.same_page`), never by host and path alone.
- The scout opens no browser and presses nothing. `run.queue_hit` does
  exactly what `/capture` does, so a scouted job meets the same two
  checkpoints as a captured one; it runs from the "Add to autopilot"
  button, and from `run.check` for a watch that is not `notify` when the
  verdict is `ok` or `caution` (Phase 17b). A notify watch never queues.
- The code filler (`browser/forms/`) clicks no option, radio or button
  without `describes_submit` refusing first, fills no field that reads as
  a visa question whatever the profile holds, and records nothing about
  such a field in a snapshot.
- No file under `applications/` is ever deleted or overwritten. Reviewer
  decisions are the exception and append, since changing your mind is part of
  the record.
- The agent never closes a job. A poor-fit verdict is a recommendation that
  still stops at checkpoint 1; only the human sets `skipped`.
- A fill is only reported as filled if the tailored resume is on the form:
  read back off the input or its block after the code upload, or from the
  agent's own `upload_file` in the action log. A screenshot proves the
  browser was alive; the model's claim that the resume is attached proves
  nothing. Jobright uploads the applicant's resume under the same filename,
  so a name match alone never proves it is ours; ours is set after theirs
  is removed.
- browser-use never launches the browser. `browser/chrome.py` starts Chrome
  detached on port 9333 and hands over a CDP URL; a remote browser is only
  ever disconnected from, so the window with the filled form stays open.
- The agent never answers a visa, sponsorship, work-authorisation, OPT, or
  citizenship question: click, input, and select_dropdown are refused on any
  field whose label (or an ancestor's, for radios) reads that way, and the
  answer step refuses the question too. Whatever Jobright's autofill set
  from the applicant's own profile is left as it is.
- The browser model never writes prose. Open questions go to the tailor
  model through the `answer_question` action; the agent types the reply.
- `upload_file` lands only on the input the model pointed at, never on
  browser-use's "nearest file input" fallback; the resume is refused on a
  cover letter input and the letter on anything else.
- A screen verdict is advice. It never sets `skipped`, never blocks
  `/capture`, never stops the pipeline. Its flags are checked in code: a
  quote that is not in the posting, or that is a question, is dropped; the
  verdict is recomputed from the surviving flags; with facts derived from
  the resume, visa, clearance, export-control, and timeline flags are soft.

## Phases

| # | Work | Estimate |
|---|---|---|
| 0 | BasicTeX install, compile `base/resume.tex` end to end | 0.5 day |
| 1 | Capture extension plus queue server | 0.5 day |
| 2 | Tailor, compile, archive, diff | 1.5 days |
| 3 | Review web page, checkpoint 1 | 1 day |
| 4 | Browser fill loop, checkpoint 2 | 2-3 days |
| 5 | Glue, `start` command, index, retries | 0.5 day |
| 6 | On-page auto-reject screen, profile switch | 1 day |
| 7 | Profile interviewer: stories, index, Profile tab, streaming text chat | 3 days |
| 8 | Voice for the interviewer: local STT/TTS, the orb | 1.5 days |
| 9 | Behavioural rundown (non-project stories) | not scoped |
| 10 | Packaging: Tectonic, first-run setup, signed installer | not scoped |

Phases 0 to 4 are built and have run end to end against real postings
(Greenhouse and Ashby). Phase 5 is mostly done: capture starts the pipeline,
approval starts the fill, `index.csv` is written, the tailoring loop retries
with specific feedback, and a failed job is restartable from the page.
Resume-after-crash is not built.

Built since, all verified on real runs:

- Cover letter per application, from the tailored resume, shown and uploaded.
- Named uploads (`AUTOPILOT_RESUME_FILENAME`, `AUTOPILOT_COVER_LETTER_FILENAME`).
- Free-form questions answered by the tailor model, archived to `answers.md`.
- Location pinned to `AUTOPILOT_LOCATION` on every location field.
- Visa / work-authorisation guard, in code.
- Terse rationale and fill notes on the review page (caveman style, after
  github.com/juliusbrussee/caveman); the resume and letter stay full prose.
- Phase 6: the on-page screen. Open a posting on Jobright or click its
  Apply and the page gets an OK / NOT OK / CAUTION bar within seconds, with
  the posting's own words per flag. Verdict cached per URL, archived as
  `screen.json`, shown on the review page. OK and CAUTION queue the job on
  their own off Jobright.
- Applicant facts and `base/stories/` fed to the tailor, letter, answers,
  and screen once a story exists; resume-only until then, with screen
  facts derived from the resume and cached. The header says which.
- Phase 9: the review page redesigned around the one decision (see below).

## What the first real run cost us

Every one of these was invisible until the pipeline ran against a real resume
and a real posting. Recorded because each was a wrong assumption, not a typo,
and the same class of mistake is likely in what remains.

- **Wrong TeX engine.** lualatex was the default. The common resume templates
  are pdfTeX-native — `\input{glyphtounicode}` and `\pdfgentounicode` are
  pdfTeX primitives LuaTeX does not provide. The engine is now chosen per
  document, on comment-stripped source, because a commented-out `\setmainfont`
  in the preamble was enough to pick the wrong one.
- **BasicTeX is genuinely minimal.** Overleaf has every package; BasicTeX
  ships about 60. Expect a `File \`x.sty' not found` per unusual package on any
  new template. `fullpage` is not even its own package — it lives in
  `preprint`. The compile error now parses the missing names and prints the
  `tlmgr` command.
- **Reasoning tokens come out of the answer budget.** A thinking model spent
  first 8k and then 32k tokens deliberating and returned a null `content`,
  which surfaced as a `TypeError` deep in a regex. Reasoning effort is capped
  and a null content now names `finish_reason`.
- **Strict length rules invite a no-op.** Squeezed by the one-page constraint,
  the model's safest move was to return the file untouched. That is now
  rejected and retried.
- **Vague retry feedback wastes attempts.** "It is too long" left the model
  guessing which of eleven bullets to cut, and it guessed wrong three times
  running. Handing it the arithmetic — which bullets grew, by how much, how
  many characters to cut — turned the next attempt into an edit and it passed.
- **Chrome will not share a profile directory.** Pointing `user_data_dir` at
  the everyday profile makes Chrome silently fall back to a throwaway, so
  every run started logged out. The fill loop owns a dedicated profile now,
  with `AUTOPILOT_CDP_URL` to attach to a browser the user already has open.
- **A text-only browser model fails totally and quietly.** Every step 404s on
  the screenshot and nothing is filled. A vision model that mis-formats an
  action fails the same way: `glm-5v-turbo` emitted `{"click": [3713]}` where
  `{"index": 3713}` was required, so every step failed validation.
- **Closing the browser destroys the checkpoint.** The fill ended with
  `browser.kill()`, which closed the window holding the completed form — the
  exact thing the human is meant to read and submit. The session is stopped
  now, never the browser process.
- **browser-use kills any browser it launched, `keep_alive` or not.** Swapping
  `kill()` for `stop()` was not enough: the local-browser watchdog answers
  every stop event with a kill and never reads `keep_alive`. So browser-use
  no longer launches anything. `browser/chrome.py` starts Chrome detached, on
  a fixed debugging port, and hands over a CDP URL; a remote browser is only
  ever disconnected from. Later runs find it still listening and reuse it.
- **`upload_file` refuses undeclared paths, and the model covers for it.**
  The tailored resume was never in `available_file_paths`, so every upload
  was refused — and the agent then reported "the resume is attached", meaning
  Jobright's generic one. The path is declared now, the task says to remove
  the autofilled file first, and `filled` requires a successful upload of
  that exact path in the action log, not a claim in the done text.
- **`upload_file` has a fallback that picks the wrong input.** When no file
  input sits near the chosen element it uploads to the input nearest the
  scroll position; on Greenhouse that was the cover letter slot. The action
  is wrapped to refuse in that case, so a file only lands where the model
  pointed.
- **Reporting success on weak evidence is worse than failing.** That failed run
  was recorded as `filled` because a screenshot file existed, which sent the
  reviewer to inspect a screenshot of nothing and left no diagnosable trace.
  Rejected tailoring candidates and the fill log are now kept and surfaced on
  the review page.

## What the second day exposed

- **A green suite means nothing for the browser.** Repeated from day one,
  because it held: `keep_alive`, `available_file_paths`, the upload
  fallback, and the positional bullet match each passed every test and
  failed on the first real form.
- **The model covers for a refused action.** With the resume upload refused,
  the agent reported "the resume is attached". Anything that matters is now
  read from the action log, never from the done text.
- **Reordering is allowed, so the length check must not be positional.** A
  reordered project read as one bullet growing by 135 characters and the
  retry feedback sent the model after a bullet that had not changed. Bullets
  are paired by text similarity now.
- **A cleared error has to be cleared.** A successful rerun left the queue's
  `error` from the failed run in place, and the page showed "Failed" above a
  good resume.
- **Escaping in one pass.** The LaTeX escaper re-escaped its own
  `\textbackslash{}` output. Caught by the test written for it, before a
  letter shipped.

## Deferred — noted, not built

- **Scout: Meta, LinkedIn, a browser fallback.** Meta's careers site is
  GraphQL behind a per-session token; LinkedIn has no public listing API;
  a careers page rendered only by JavaScript needs a browser. All three
  are refused by name at add time instead of half-working. A Playwright
  provider would cover the last one at the cost of a browser in the
  scout, which is what the one-click thesis says to avoid.
- **Scout: Telegram / push.** Mail was asked for; the digest is one
  function (`mail.digest`) and a second channel is one more sender.

- **Recipe cache per ATS domain.** Not built; every fill currently runs the
  full agent loop at full cost, roughly 20-40 model calls with a screenshot
  each. The plan: on first encounter with a domain, log the successful action
  sequence; on later jobs from that domain, replay it as a deterministic
  script; fall back to the agent loop when replay fails. Worth building once
  fill quality is proven, not before — caching a sequence that does the wrong
  thing just makes it fast.
- **Hardcoded selectors instead of an agent.** A cheaper, faster alternative to
  the recipe cache for the parts that never vary: Jobright's autofill button,
  and the field names on the two or three ATSes used most. Rejected for v1
  because it is per-site work that breaks on any DOM change, and the agent
  loop covers a novel ATS with no code at all. Reconsider if the same two or
  three domains turn out to account for most applications.
- **Queue file-watcher auto-trigger.** Superseded: `/capture` starts
  `pipeline.py` directly, and a job captured while the server was down gets a
  "Tailor it now" button.
- **Auto-filling on approve.** Reverted once because a dead browser left the
  job stuck in `filling`. Now on by default (`AUTOPILOT_AUTOFILL=1` in .env)
  since the fill is restartable from any post-approval state and the browser
  is no longer killed by the run ending.
- **Concurrent fills.** Approve started a fill and "Fill the form now" started
  a second one three seconds later; both drove the same tab, each step
  invalidated the other's element indices, and the log showed every step
  twice. `server/runner.py` now tracks the live `apply.py` per job (with a
  `pgrep` fallback for one an earlier server started), refuses a second, and
  the page hides the buttons for 30 seconds after a start. A restart is
  double-gated behind `fill_running` and a confirm.
- **Closing the browser.** Each fill opens its own tab, and a fill still
  running after the human had submitted by hand kept reopening the page. The
  browser is now closed by the server on "I submitted it" when nothing else
  needs it (`browser/chrome.py: close()`), and kept, with the page saying
  what is in progress, otherwise. A hand submission during `filling` kills
  the agent first.
- **Resume-after-crash.** A fill interrupted halfway leaves the form partly
  filled and the job restartable, but nothing reconstructs where it got to.
- **Cheaper vision for the fill loop.** Tried and rejected so far:
  `z-ai/glm-5v-turbo` (mis-formats actions), `deepseek/deepseek-v4.1-flash`
  (accepts images on OpenRouter, but browser-use forces `use_vision=False`
  for any DeepSeek model, so the agent runs blind and stalls after
  autofill), `qwen/qwen3.7-flash` ($0.03/M, but 75-second timeouts on every
  step after autofill inflates the DOM). `z-ai/glm-5.3-flash` is set and
  untested; `google/gemini-3.1-flash-lite` is the known-good fallback. The
  real cost lever is the recipe cache above, not the model.
- **Checking the location field after the run.** The location rule is
  prompt-level; nothing reads the DOM back to confirm. The done text carries a
  `location:` line for the reviewer instead.
- **Glasses as the checkpoint surface.** The long-term goal is for both
  checkpoints to render on glasses: the resume diff appears, it is confirmed
  or rejected there, and the pipeline continues without touching a keyboard.
  This is why checkpoint 1 is a web page rather than a terminal prompt. No
  glasses-specific work is in scope for v1.

## Phase 6 — on-page screening (built)

The problem: a good share of postings are auto-rejects before any tailoring
is worth doing — a visa or export-control line, a years-of-experience bar,
a start date that cannot be met, a country the candidate is not in. Today
that is found by reading the whole posting, or worse, after the resume has
been tailored. The fix is a verdict on the page itself, seconds after it
opens, before any decision to capture.

### Behaviour

1. A job page opens (from Jobright, or any known ATS domain). The capture
   extension's content script waits for the DOM to settle, takes the page's
   visible text, and posts `{url, title, text}` to `POST /screen`.
2. The server answers from cache if the URL was screened already, otherwise
   asks a fast, cheap model for a verdict against the applicant's facts.
3. The extension paints a banner across the top of the page: red for a hard
   auto-reject, purple for a caution, green for clear, grey if the page is not
   a posting. Each flag is one line: category, the posting's own words, why it
   applies. Add calls the existing `/capture`, then focuses the existing
   Autopilot tab on that job or opens one if none exists. After five seconds
   the banner minimizes to a verdict-coloured assistant badge; restoring it
   reuses the rendered result and makes no model call.
4. The pipeline reuses the same verdict: `pipeline.py` screens the fetched
   posting (cache hit when the extension already did), archives it as
   `screen.json`, and the review page shows the flags above the rationale.

### Categories (v2)

Fixed set, returned by name so the page can colour and the reviewer can
grep. The first weeks of real screens showed the v1 table rejected
stretches: another US city, a cohort one year off, 1.5 years against
"0-1", a skill the resume did not list. A screen exists to catch the
handful of postings that are certain to be thrown out, so each category in
`screen_rules.md` now lists three things: what is `hard` (certain), what
is one `soft` line (apply anyway, but know), and what is never a flag.

| Category | Hard | Never |
|---|---|---|
| `experience` | Required minimum two or more years above, no "or equivalent" | Over-qualified; no number; years met once internships and research count in full |
| `visa` | Sponsorship refused now and in future, and needed | The form question; "sponsorship available"; E-Verify text |
| `export_control` | ITAR / EAR / "US persons only" for this role | Company-wide "some roles may" notes |
| `clearance` | Active clearance, citizenship, or residency required | The form question; "or" clauses with a path |
| `timeline` | Enrolment after graduation; a start date or term the facts rule out | A window the latest degree falls in; graduation on or before an "earned or expected by" cutoff; a cohort year in the title alone |
| `location` | Outside the applicant's country; remote restricted to a region they will not move to | Any location in the United States, including onsite/hybrid, no relocation assistance, and state-restricted remote; several offices listed; HQ in a remote header |
| `degree` | PhD or a licence required, no equivalent | "CS or related" when held; "MS preferred" when held |
| `seniority` | Staff, principal, director, manager with scope stated | Level names (II, L4); a company's own levelling vocabulary |
| `perm` | Two or more PERM tells: mail-in resume, job code, single exact salary, "labor certification", a named HR contact | A plain salary range |
| `other` | A human language; a physical requirement; "internal only" | Skills, domains, salary, posting age |

The verdict is `reject`, `caution`, `ok`, or `not_a_job`, recomputed from
the flags in code. US location flags are dropped at validation, and a parsed
graduation date on or before a parsed latest-date cutoff drops that timeline
flag. Derived facts carry relocation and graduation lines, so these checks do
not depend on the model's prose reasoning.

### Where things go

| Path | What |
|---|---|
| `base/applicant.md` | The applicant's hard facts, one `## Facts` section the screen prompt reads verbatim. Gitignored; `base/applicant.example.md` is the template. Already read by `browser/fill.py` for form details. Phase 7 grows this file |
| `tailor/screen.py` | `screen(posting_text, applicant) -> Screen`; JSON reply parsed and validated against the category enum, never trusted raw |
| `tailor/screen_rules.md` | The prompt, sent verbatim, same convention as the other rules files |
| `server/screen.py` | `POST /screen` and the URL-keyed cache in `data/screens.json`; employer pages reuse the Jobright URL's cached verdict, or combine saved Jobright text with a weak employer page in one call |
| `capture/content.js` | Text extraction, banner, persistent badge, posts to the server, and Add/Open routing to the matching review job. Runs on `jobright.ai/jobs/info/*` (verdict only) and, via `background.js`, in any tab opened from or navigated away from Jobright, wherever Apply lands (verdict, then a three-second countdown into `/capture` for `ok` and `caution`, with auto-approve and "Use Opus" decided inside it) |
| `applications/<job>/screen.json` | The verdict the pipeline archived |
| `review/index.html` | Flags shown above the tailoring verdict; the profile state in the header |
| `server/settings.py` | `data/settings.json`, read by the server and the scripts it launched. `use_profile` is pinned on by the page; the models fall back to the resume on their own |
| `tailor/profile.py` | What the models know about the applicant: profile.md, plus applicant facts and story documents when the switch is on; derived facts for the screen otherwise |

Model: `OPENROUTER_SCREEN_MODEL` in `.env`, defaulting to a small fast model.
This is a classification over a few thousand tokens; the tailor model is
overkill and too slow for a banner.

### Not built, on purpose

Per-site selector profiles, with browser-use grabbing the description on an
unknown site, were considered and dropped. The content script reads the
rendered page in the user's own Chrome, so layout never matters: whatever
the employer's ATS, `innerText` is the posting, and the model answers
`not_a_job` when it is not. A selector per domain would be work that buys
nothing until a site hides its text (Workday's iframes are the likely first
case); add a rule for that site then.

### Invariants added

- A screen verdict is advice. It never sets `skipped`, never blocks
  `/capture`, and never stops the pipeline. The human decides; the banner
  only makes the decision faster.
- The screen reads the posting and the applicant's facts. It never writes
  to a form and never answers anything, so the browser guard is untouched.
- The extension sends page text to the local server only. Nothing leaves
  the machine except the model call the server already makes.
- The screen never runs against empty facts. With `base/applicant.md`
  absent (or the profile switch off) it uses facts derived once from the
  resume, cached until the resume changes, and says so on the banner: a
  resume knows nothing about visas or start dates.

### Status

All five steps built. What the first runs exposed:

- **The model's verdict disagreed with its own flags**, so the verdict is
  recomputed from the flags in code.
- **Fabricated quotes.** `deepseek-v4-flash` wrote "no statement about
  clearance" as a quote and flagged it hard. Every quote is now checked
  against the posting text; not found, dropped.
- **A form question is not a requirement.** "Will you now or in the future
  require sponsorship?" was flagged as a visa reject by two models running.
  The rule is in the prompt and, since prompts leak, in code: a quote
  ending in `?` is dropped.
- **Prompt-only location and timeline rules leaked.** The model cautioned on
  San Jose versus Los Angeles despite acceptable relocation, and cautioned on
  December 2026 against an "earned or expected by Summer 2027" deadline. US
  location flags and already-satisfied latest-date graduation flags are now
  removed deterministically before the verdict is recomputed, including when
  an older location result is read from cache.
- **Reasoning tokens made a 32-second banner.** The screen runs with
  reasoning disabled (`llm.complete(reasoning={"enabled": False})`); the
  tailor model's provider refuses that, so derivation uses the screen model.
- **Screening ran on LinkedIn only.** The match list plus "tabs opened from
  jobright.ai" missed where Apply actually lands: an employer domain, often
  by same-tab navigation. `background.js` now marks any tab opened from or
  navigated away from Jobright or LinkedIn and injects wherever it stops.
- **A missing applicant.md was a loud error**; now it is a fallback. Facts
  are derived from the resume once, the bar says so, and the flags a resume
  cannot judge are soft.

Model: `deepseek/deepseek-v4-flash` ($0.066/M in), 1-5 s per posting.

- **Every other city was a reject.** Seven of the first 42 screens flagged
  location, four hard, all for SF / Seattle / NYC / Newark against an LA
  address, because the derived facts said nothing about relocation. Nine
  flagged a graduation window the latest degree fell inside, one flagged
  1.5 years against "0-1". The v2 categories above and a relocation line
  in the derived facts are the fix; the rules now say what is never a flag.
- **Screening everywhere was noise.** The bar on every ATS domain and on
  LinkedIn screened pages nobody was going to apply from. The extension
  now covers the Jobright flow only, and queues clean verdicts itself:
  the person's job is to look at the bar, not to click after it.
- **Two URLs, one job.** A Jobright posting page and the employer page it
  leads to are the same job. Auto-queue is off on Jobright's own pages so
  the pipeline runs once, on the employer URL the fill will need anyway.

Not yet seen on a real run: the v2 rules and the auto-queue countdown on
a live Jobright session.

## Phase 7 — profile interviewer (built)

The tailor and answer steps work from a one-page resume and a thin profile.
The experience behind the resume is not on it: what was built, why, what
broke, the numbers, which part was the candidate's own. Without that the
tailor can only paraphrase bullets; with it, it can swap a project in,
rewrite a bullet around a fact the posting cares about, and answer a form
question with a real story. The same material, read back before an
interview, is the candidate's prep.

### Decisions

- **Surface**: a "Profile" tab on the review page. The interviewer lives
  there, and so do the finished documents, so they can be reread later.
  Text now; the reply streams token by token so the voice layer (Phase 8)
  only swaps input and output.
- **Seed**: `base/resume.tex` is read directly. A free-text box on the tab
  takes anything else the candidate wants to hand over (notes, an old
  resume, a project README, `.txt` / `.md` pasted or uploaded). Extra
  text is used once, to seed the interview, then discarded; once the main
  document exists there is nothing in it the document does not hold.
- **Opening turn**: the interviewer lists the experiences it found on the
  resume and the roles the questions will target (SWE, backend,
  full-stack, data, ML, infra), asks whether anything is missing from
  either list, then goes one experience at a time: roles first, then
  projects, then "anything not on the resume?".
- **Question bank**: `tailor/interview_rules.md`. A fixed checklist per
  experience (nine lines for every experience, three more for roles,
  three more for candidates past their first year), plus domain probes
  the model picks from the candidate's own answers, not from the target
  role. The rules file is the prompt; edit it, not the Python.
- **Enough**: every checklist line covered, or ten questions asked, or the
  candidate says done. Then "anything about this one I missed?" and on to
  the next. Uncovered lines are written as "not discussed"; the tailor
  never fills them in.
- **Skipping**: the first question for an experience is open ("tell me
  the story of this work"); the model marks every line that answer
  settled and asks only about what is left. A long answer covering
  several lines skips several questions.
- **Models**: the light model (`OPENROUTER_INTERVIEW_MODEL`, default the
  screen model) runs every turn and writes the main document. The heavy
  model (`OPENROUTER_TAILOR_MODEL`) writes the two children afterwards.
- **Documents**: `base/stories/<slug>/main.md` is the source of truth,
  organised by checklist line, in the candidate's words. Two children
  derive from it: `tailor.md` (dense facts for the tailor, cover letter,
  and form answers: dates, stack, scale, numbers, own contribution, one
  candidate bullet per notable fact) and `star.md` (for the candidate:
  the story in Situation / Task / Action / Result / Reflection form, the
  numbers to say aloud, the questions an interviewer is likely to ask and
  the line that answers each, the gaps to look up). No rehearsal script;
  `star.md` holds all the content, so the candidate or any other model
  can build answers from it. Everything under `base/stories/` is
  gitignored.
- **Updates**: the assistant is ever-living. The candidate tells it new
  information in the same chat ("I also did X on project Y", "the number
  was 40 percent, not 30"), it updates `main.md`, and regenerates both
  children with the same rules. Hand-edited children get overwritten by
  the next regeneration; the main document is the thing to correct. The
  candidate never hands documents in.
- **Boundaries**: the interviewer writes only under `base/stories/`. It
  never edits `base/resume.tex` or `base/applicant.md`.
- **Resume anywhere**: state per experience in
  `base/stories/<slug>/state.json` (transcript, coverage). Stop mid-way,
  come back, the next question picks up where it left off.
- **Feeding the tailor**: `base/stories/index.md` is regenerated with the
  children, one line per experience (slug, one-sentence summary, stack),
  and is always in the prompt. Before a tailor run a cheap call picks the
  three or four slugs that match the posting; only those `tailor.md`
  files are appended. The cover letter and form answers reuse the pick,
  recorded in the application folder as `stories_used.txt` and shown on
  the review page. This replaces the flat 24k-character cap; nothing
  outside `tailor.md` and the index ever reaches a job prompt. Done in
  code, not with model tool calls: the tailor is one completion with a
  retry loop, and tool calling has already failed once on a cheaper
  model.
- **Behavioural material** (conflict, failure, feedback, "why this
  company") is not part of this interview; collecting the experiences is
  long enough. It is Phase 9, a separate rundown the candidate can opt
  into, writing its own main document and `star.md`.

### Build order

1. Stories layout, index, picker in `tailor/profile.py`; `stories()`
   reads only `tailor.md` files. Tests for the picker with a stubbed
   model.
2. `tailor/interview.py`: the loop. One turn = transcript + checklist in,
   coverage update + next question out, as JSON the page can render.
   State on disk. Main document written when an experience closes.
3. `server/`: `/profile/*` endpoints, streaming turn over SSE.
4. Profile tab in `review/index.html`: seed box, chat, list of
   experiences with coverage, documents readable in place.
5. Children generation with the heavy model; regeneration on update.
6. Real run against the real resume before anything is called done.

### Status

All six built, 2026-09-15, and run against the real resume: five
experiences found, first one interviewed and closed, `main.md`,
`tailor.md`, `star.md`, and `index.md` written, the second experience
opened. What the first real run changed:

- The model sometimes skips the header on a short turn. The code then
  grades the answer in a second cheap call rather than lose coverage.
- "Nothing more, move on" got another question. A move-on phrase from the
  candidate now closes the experience in code, whatever `DONE` says.
- The children thread saved `state.json` while a turn was reading it, and
  the reader saw an empty file. Every state write is now atomic.
- The heavy model copies the code fence from `story_rules.md` around the
  whole tailor document. The fence is stripped on save.
- The model marked "Numbers" covered from the resume entry's own figures.
  The reply format now says the resume entry covers nothing.

Model comparison, same prompts, real resume, three rounds each
(`deepseek/deepseek-v4-flash` vs `z-ai/glm-5.3-flash`, 2026-09-15):

| | DeepSeek Flash | GLM 5.3 Flash |
|---|---|---|
| Experiences found | 5/5, 2 companies | 5/5, 2 companies |
| "experienced" (resume is two internships) | true, wrong | false, right |
| Header present | 9/9 | 9/9 |
| Coverage judgement | generous: "reported to the lead" marked Day to day; once marked all 15 lines on "move on" | strict: only what the answer said; nothing on "move on" |
| Turn latency | 0.6-3 s | 3-5 s (one 19 s outlier); reasoning is mandatory on this endpoint |
| main.md | clean | leaked "move on" into a section once |
| Cost per experience | $0.0016 | $0.0022 |

GLM is the default (`OPENROUTER_INTERVIEW_MODEL`): coverage drives which
questions get asked, and over-marking hides gaps for good. The move-on
over-marking is also blocked in code now. DeepSeek is the choice if the
voice loop feels slow.

Not built: partial transcripts while the candidate speaks (the recording
is transcribed on release). A closed experience cannot be reopened from
the page; corrections go through the open-phase chat, which rewrites
`main.md`. Seed files: PDF, text, markdown, `.tex`, several at once,
extracted server-side (`POST /profile/seed-file`, pypdf); a scanned PDF
is refused with a reason.

## Phase 8 — voice (built)

Local, on-device, in line with the one-click thesis. `voice/` is three
small modules and no Python ML dependency:

- **In**: the page records raw PCM, resamples to 16 kHz and encodes a WAV
  itself, so the server needs no ffmpeg. `POST /voice/transcribe` runs
  `whisper-cli` (whisper.cpp) on it. The text goes into the same turn a
  typed answer would.
- **Out**: the streamed reply is split into sentences as it arrives; each
  is posted to `POST /voice/speak` and the page plays them in order, so
  speech starts on the first sentence. The backend is **Kokoro** through
  ONNX Runtime (`voice/kokoro.py`, in process, model kept loaded; the
  `voice` extra in pyproject, no torch; ~1.5-3 s per sentence on an M-series
  CPU, fp32 is faster than the int8 file on arm64). It needs a working
  espeak-ng for phonemes: the one bundled with `espeakng-loader` ignores
  its data path on Apple Silicon and calls `exit(1)` from C, so it is never
  loaded in the server; Homebrew's `espeak-ng` is used and probed once in a
  child process. Without Kokoro the backend is macOS `say`
  (`AUTOPILOT_SAY_VOICE`), which the user judged not good enough. Piper
  was the first plan: the 2023.11.14-2 release's "macos_aarch64" tarball
  is an x86_64 build and the `piper-tts` wheel has the same espeak bug;
  `AUTOPILOT_PIPER_BIN` still switches to it when a working binary exists.
  Pocket TTS (Kyutai) was considered and rejected: it pulls torch.
- **Models** live under the app data directory (`voice/` next to the
  Chrome profile) and download from the page on first use with a progress
  bar: the whisper `base.en` model (148 MB) and, once the voice extra is
  installed, the Kokoro model and voices (325 MB + 28 MB). whisper.cpp publishes no macOS
  binary, so until the app bundles one it comes from `brew install
  whisper-cpp`, and the page says so. Packaging (Phase 10) ships it.
- **Fallback**: with a half not set up, the page uses the browser's own
  Web Speech API for that half, so the interview is never blocked on a
  download. `whisper` on PATH is the openai-whisper Python CLI and is
  deliberately not picked up.
- **The orb** ("Voice mode" in the voice bar): one element, four states
  (idle breathing, listening green with the mic level, thinking spinning,
  speaking amber with the playback level from an AnalyserNode), captions
  under it. Hands-free loop in the page: tap to talk, silence of 1.5 s
  ends the turn (RMS above 0.015 counts as speech; 8 s of nothing gives
  up without sending), the reply is spoken sentence by sentence, then the
  mic reopens. Tap while speaking interrupts. Two sizes: full (chat
  hidden) and mini (84 px orb docked above the chat, text box usable),
  plus "Back to text". Voice mode forces replies aloud. Mode remembered in
  `localStorage`.

The interview loop did not change: a spoken answer is the same
`/profile/turn` as a typed one.

### Testing notes

What to try, in order, with the real resume:

1. Text only: Start, confirm the list, answer three or four questions, say
   "move on"; check the nav coverage, `main.md`, then `tailor.md` and
   `star.md` a minute later, then `index.md`.
2. Same in voice mode, full orb, hands-free: does the silence cut-off fire
   too early on a thoughtful pause? (SILENCE_MS in the page.) Does the
   interrupt work?
3. Mini orb: talk while reading the transcript.
4. Open phase: "the number was 40 percent, not 30" → `main.md` rewritten,
   children regenerated; "I also built X" → new experience interviewed.
5. Capture a job with the profile switch on: `stories_used.txt` in the
   folder and "Stories the tailor read" on the review page.
6. Reload mid-interview: the next turn continues where it stopped.

## Phase 9 — the review page around one decision (built)

The page grouped jobs under nine status headings in one accent colour, and
the decision was a row of small buttons above a long page. The person's
job on this page is to look at two PDFs and press one button; everything
else is context.

### Decisions

- **Three buckets, not nine.** In flight (queued, tailoring, approved,
  filling, awaiting review, filled), applied, rejected. Only in flight is
  open; applied and rejected are shelves that open on a click and never
  stay open across a reload.
- **Colour is state.** A row is washed purple while the agent has it and
  green when the next move is the person's; the detail pane takes the
  same wash. Each in-flight row ends in one word: `tailoring`, `filling`
  (with a live ellipsis) or `Approve`, `Submit`. Applied is green,
  rejected red, both on the shelves only.
- **The PDFs first, the button floating.** Resume and letter side by side
  at the top of the pane; a large Approve (or "I submitted it") fixed
  bottom right with Reject small beside it. Rationale, diff, instruction
  box, posting, log and history below.
- **Terminal look.** Monospace, square corners, 1-2 px rules, black on
  white with a dark theme. Three directions were mocked (editorial,
  terminal, graphite) with three row-state treatments (rail and chip,
  stepper, wash and verb); the person chose terminal and wash-and-verb.
- **No internals.** Model names, pids, folder names, and python commands
  are gone from the page; the voice bar says ready or not rather than
  naming backends. The "Use profile" switch became a header fact,
  "Profile not set up yet" / "Profile used for tailoring", that opens the
  Profile tab; `use_profile` is pinned on because the models already fall
  back to the resume when no story exists.
- **The on-page bar matches.** Black, monospace, one verdict colour on
  the tag and the rule; purple while the screen runs.

### Status

Built and checked with headless screenshots on dummy rows; not yet used
through a full capture → approve → fill → submitted cycle after the
redesign. The Profile tab took the tokens (mono, square) but its layout
is unchanged and is the next thing to restyle.

## Phase 10 — the capture without an extension (prototype)

The extension was the one step of the one-click thesis with no clean
answer. Installing an unpacked extension needs Developer mode and a
folder pick; `--load-extension` is gone from branded Chrome since 137;
policy force-install is ignored on unmanaged Macs; the Web Store is a
review queue, and then still a click. Meanwhile the Chrome the fill runs
in is already ours (`browser/chrome.py`), and Jobright is already logged
in there, so the capture can live in that Chrome without installing
anything.

`browser/inject.py` attaches to the debugging port, watches every tab
(`Target.setDiscoverTargets` + `setAutoAttach`), and evaluates
`capture/content.js` on load in the tabs that matter: Jobright, and
whatever Jobright's Apply opens. The extension code stays in `capture/`
untouched and still loads the old way; the same `content.js` serves both.

### What the first run taught

- **Jobright's Apply tab has no opener.** "Apply with autofill" is
  Jobright's own extension creating the tab (`chrome.tabs.create`), so
  there is no `openerId` and no `Page.windowOpen`. The URL it opens is
  tagged `?jr_id=<posting id>`; that tag is how the tab is recognised
  (`SOURCE_MARKS`). Plain links still go by opener.
- **A main-world `fetch` to 8787 can hang forever.** Oracle's ATS page
  registers a service worker that swallowed the call. An extension content
  script never met this. The page now talks to the server through a CDP
  binding (`Runtime.addBinding`, `__autopilotRequest` /
  `__autopilotReply`): Python makes the HTTP call. The page's CSP, workers,
  and fetch overrides are out of the loop, and the server is never
  reachable from page JavaScript at all. Without the binding (the
  extension path) `content.js` falls back to `fetch`.
- **Injecting at `load` is earlier than `document_idle`.** Oracle's page
  is an empty shell at load and fills in from an XHR seconds later;
  `content.js` saw 200 characters and gave up for good. It now waits for
  text, a second at a time, for up to twenty, then paints "No posting"
  with an Again button (the hint had promised one that did not exist).
- **Jobright's posting page now presses Apply itself.** An ok or caution
  verdict counts down and presses Jobright's Apply; the employer tab
  screens and queues on its own. A reject waits for the click. Nothing on
  a form is ever pressed: the button matched is the one on the posting
  page, and it opens a tab.
- **Some Apply buttons land on a bare form.** No description to fetch,
  and ATS pages render client-side anyway. `server/postings.py` keeps
  Jobright's copy of the posting (saved when its page is screened, keyed
  by posting id) and the employer page's rendered text (sent with the
  capture). `pipeline.fetch_posting` uses the fetch when it has a real
  description, else the employer page's text, else Jobright's. The screen
  also reuses Jobright's cached verdict, or sends the saved Jobright copy and
  the weak employer page together in one call. Role and company come from
  Jobright's `<role> @ <company>` title when present, otherwise from the
  visible company / age / role lines after "Original Job Post".

### What the second day taught

- **The injector's socket drops.** Thirty seconds after a new tab, no
  close frame, cause unknown (the fill's own attach is the suspect). The
  process died and every tab lost its banner until someone looked. It now
  reconnects with backoff for as long as port 9333 answers and exits only
  when Chrome is gone; keepalive pings are off.
- **`window.open` from an evaluated script is at the site's mercy.**
  Ashby swallowed the "Open in autopilot" click. The page now asks the
  bridge (`/__open`); the injector focuses and navigates an existing
  Autopilot tab or creates one over CDP when none exists. Add, Add anyway,
  Open in autopilot, and the extension context-menu capture share that rule.
  Only the configured local server origin is allowed through the bridge.
- **Port 8787 is not guaranteed to be free.** `AUTOPILOT_SERVER_URL` selects
  the injector's local server origin (8788 in the conflicting-service run)
  and rewrites the injected content script to the same origin. The unpacked
  extension remains pinned to its manifest's 8787 permission.
- **The Jobright page still got queued once**, forty minutes after the
  client-side guard shipped. The server now refuses `jobright.ai` at
  `/capture`; client-side guards are convenience only.
- **The close countdown started before the job existed.** On Jobright's
  page the employer tab does the queueing; the bar now polls `/queued`
  by `jr_id` and counts down only once the row is there.

### Status

Run by hand with `python -m browser.inject` (a Herdr pane). Verified
live: posting page → countdown → Apply → employer tab screened → queued
with its text → pipeline reached review on a page the fetch could not
read; a filled form's tab reports the confirmation page and the job is
marked submitted without the button. Next, in order:

1. `server/runner.py` starts the injector with the server (one process,
   restarted if it dies; `Chrome quit` is the normal way it dies) so
   nothing is run by hand. A `tools/up.py` that starts server, Chrome,
   and injector and opens the review page is the interim.
2. Retire the extension path once the injector has done a week of real
   captures: `capture/background.js` and the manifest go, `content.js`
   stays as the injected script, the `fetch` fallback in it goes.
3. Facts are derived from the resume until the facts interview
   (Phase 11) has been run once; the "Unchecked: visa and dates" line
   stays until then.

## Phase 11 — already seen, the facts interview, deterministic steps (built)

Three things the second real day asked for, all in the direction of
fewer model calls and fewer duplicate actions.

### Already in autopilot (`server/seen.py`)

One job was tailored twice: once under its Jobright URL, once under the
employer's. The queue deduplicated on exact URL only. Now two levels,
both string work, no model:

- **high**: the same posting, provably. Same canonical URL (host
  lowercased, `www.` and tracking dropped, ATS job params kept), same
  `jr_id`, or the same job id at the same tracking system (Ashby,
  Greenhouse, Lever, Workday, Oracle, iCIMS, SmartRecruiters, Workable,
  Jobright). `queue.add` refuses these; `/capture` returns the existing
  row.
- **confident**: same employer, and either the title matches at ≥ 0.8
  (noise like `[Remote]` and `(All Levels)` stripped) or the posting
  text's simhash is within 6 bits. Shown, never blocks: "Add anyway"
  stays. Rejections stop matching here after 90 days.

`/screen` carries the match as `seen`; the banner then never counts
down, says where the job stands ("Waiting for your review", "Applied
2026-09-17", "Rejected …"), and offers "Open in autopilot". Rows swept
out of the queue still count, via `applications/index.csv`.

The same lookup keys the file chips: on a page that is a known job, the
tailored resume and cover letter appear in the banner as chips to drag
onto the form's slot, click to download, or "put" straight into the
matching file input (`POST /review/{id}/files`, base64 over the bridge).

### Which copy of the posting

Three copies can reach the pipeline (fetched page, browser's text,
Jobright's copy). The first to clear 400 characters used to win, which a
bare form clears on cookie notices alone. `tailor/quality.py` strips
boilerplate lines and scores words plus section headings plus bullets;
the fetched page wins at ≥ 60% of the best, else the best copy does,
and `posting.md` records `**Text from:**`. No model.

### The facts interview (`tailor/facts.py`)

`base/applicant.md` was a file to type by hand, which fails the
one-click thesis, and its absence is why every screen said "Unchecked:
visa and dates" and guessed at relocation. It is now the first thing the
profile chat does after reading the resume: eight fixed questions in
code (authorisation, clearance, level, location, relocation, start
date, graduation, form details), the resume's own answers offered as
hints, each answer turned into one literal line by the cheap model, one
follow-up at most, "skip" leaves unknown. The file is written at the
end and the stories interview opens. "Skip to the stories" and "Redo the
facts" exist; a redo mid-stories comes back to the story it left. The
header pill shows Facts and Stories separately.

### Deterministic steps

The browser agent's first three actions on every form were the same:
find Autofill, click, wait. `browser/autofill.py` does that over raw CDP
before the agent starts: opens the form in a tab on our Chrome, finds
the one control whose text starts with "autofill" (page and every
shadow root; Jobright's panel is a custom element), checks it against
the submit deny-list, clicks once, waits for the panel to say done or
the filled-field count to hold still. The agent `switch`es to that tab
with step one marked done. Any failure = `clicked=False` and the agent
does it as before. The first of what should become a harness: the agent
does a thing, the code learns to do it, the agent is the fallback and
the source of the next flow.

`browser/ats_rules.md` is the other half: per-system notes for the
agent, picked by URL, written from what the logs showed (Oracle: one
"Upload Attachment" control for every document, Address prefilled with
India, the Jobright panel stalls at 85%; Workday: never create the
account). Edit the markdown, not the Python.

### Status

Built, 432 tests. Live: dedupe, chips, and the submission watch. Not
yet seen live: `browser/autofill.py` on a real fill (the finder was
fixed after a first run found nothing; `AUTOPILOT_AUTOFILL_BY_CODE=0`
turns it off), and the facts interview end to end (the normaliser was
tried against the real model).

## Phase 12 — our own fill, and learning from corrections (built)

The question that started it: Jobright's autofill gets most fields wrong
on the systems that matter, and the agent then corrects someone else's
work instead of doing its own. Is building the fill easier? A survey of
open source (September 2026): six repos, 0–110 stars, one developer each,
useful as selector crib sheets and nothing more. So: yes for the systems
with a stable form, and the survey's MIT repos were read for their
selectors.

### What was built

- `browser/forms/`: the fill without a model. One engine, one adapter
  per system (Ashby, Greenhouse, Lever). Scan every control over raw
  CDP with a label resolved the way a screen reader would; match by the
  adapter's id/name selectors, then `base/form.json` `answers` by exact
  label, then generic label patterns; set by kind (native setter with
  typed keys as fallback, `<select>` by option text, radios and Ashby's
  toggle buttons by click, comboboxes by click + keys + a mouse click on
  the suggestion, files by `DOM.setFileInputFiles` read back); rescan and
  hand the agent the list of what is still empty. Verified live on all
  three the day it was written; six bugs were found only there
  (CLAUDE.md, "Things that will bite you").
- Jobright's completion signal. Their extension's bundle sits in our
  Chrome profile; reading it showed the fill reports progress with
  `window.top.postMessage` (`updateResultFromIframe` snapshots,
  `autoFillResultFromIframe` with `missingFields`). `autofill.py` installs
  a listener before the click and stops on Jobright's word; the missing
  list goes to the agent.
- The correction loop (`server/corrections.py`). The form as the agent
  left it is snapshotted; the filled tab pings the server while the human
  checks it and the server looks at the form itself; marking the job
  submitted diffs the two and writes every changed or newly filled value
  into `base/form.json` `answers`. The human's fix is the next fill's
  first choice, whoever made the mistake. (Phase 16 renames the store
  `corrections`, keeps what the form held, and applies it after
  Jobright's autofill, where the wrong value comes from.)
- The preliminary interview (`server/form.py`, `Form` in the page): the
  fixed questions every form asks, click-through, contact details
  prefilled from the resume, source pinned to "Other", authorisation
  collected for the file and the screen.

### Decisions

- The filler never answers a visa question, even from the applicant's own
  file. The invariant was written against a model inventing an answer;
  code copying a human's declared answer is a different thing, but the
  field is where a wrong value does the most harm, so it stays with the
  human until decided otherwise. The keys are on file, outside
  `forms.profile.KEYS`, and `describes_protected` runs before matching.
- Jobright stays step one on Workday, Oracle, iCIMS and SmartRecruiters.
  Those are where it is worst and where it is hardest to replace: Workday
  is a multi-page wizard behind an account wall, Oracle a page of custom
  elements. Each is one adapter module when its turn comes; the engine,
  the snapshot and the correction loop are already there for it.
- Snapshots are taken by the server over CDP, not by the page. One label
  implementation; the page only says "look now".

### Status

Built, 467 tests at the time. Live: the three adapters on real forms. Not yet seen
live: Jobright's message on a real fill (the listener was checked with
their message shapes in a real tab), the correction diff on a real
submission.

## Phase 13 — Jobright first, pressed on open, and a signal (built)

Three complaints from one evening (2026-09-18): the autofill took too
long to start, nothing on the page said whether automation was doing
anything, and the minimise button on the banner did not minimise.

- **Pressed on open.** The wait was the pipeline: queue, tailor,
  approve, then `apply.py` opened a second tab and pressed. Now the
  injector, which already watches every tab opened from Jobright, runs
  `autofill.run` in the tab the moment it has loaded (`maybe_autofill`,
  once per tab+URL). The fill then finds that tab by URL and reads
  Jobright's own messages back off `window.__autopilotJR` (`reuse`); it
  presses itself only when no such tab exists. `AUTOPILOT_FORM_FILL=0`
  in `.env`: the in-house adapters step aside, Jobright is always step
  one.
- **Two-step panel.** The first live press found "Autofill my
  application", clicked it, and nothing happened, twice: on Greenhouse
  that control opens the panel and a control worded "Autofill" inside
  it starts the fill. While no message has arrived and no field has
  changed, a differently worded control gets one more press
  (`MAX_PRESSES`).
- **Frozen tab.** During that investigation a tab stopped answering
  `Runtime.evaluate` for good (`Page.navigate` accepted, ignored; no
  dialog). A native file chooser blocks the renderer the same way, so
  `Page.setInterceptFileChooserDialog` is on for the duration of the
  press. Whether that was the cause is not proven.
- **The signal.** `window.__autopilotAutomation(state, note)` in
  content.js; the injector and the fill evaluate it over CDP
  (`signal_js`, `notify`). `working` = purple pulsing core on the badge
  and a note line in the bar; `done`, `error`, `idle`.
- **Minimise.** The close button's click bubbled to the host element,
  whose own listener re-expands a collapsed bar on any click: collapse,
  then expand, in one click. `stopPropagation` on close.
- **Ports.** Caveman Cloud's proxy took 8787 under launchd and answered
  the page with `cave_route_not_found`; uvicorn had died. Killed, server
  back on 8787. `AUTOPILOT_SERVER_URL` exists for when it has to move.

### Status

Built, 475 tests. Live: the injector pressed within two seconds of load
on a Greenhouse embed, and the second step is what it was missing. Not
yet seen live: a fill reusing the opened tab end to end, the signal on a
real form, whether the chooser intercept was the freeze.

## Phase 14 — the trigger, the documents in code, no agent (built)

One evening (2026-09-18), starting from "the autofill works and then
nothing happens". Six findings, each a cycle.

- **Nothing happened because nothing was queued.** `seen.canonical`
  dropped the query, so every Greenhouse embed
  (`/embed/job_app?for=<company>&token=<id>`, one path for every job)
  was "the same page" as the last one and the banner said "already in
  autopilot". `for` and `token` are kept and the token is the ATS id.
- **The deterministic end of the autofill.** Jobright's panel lives in
  the shadow root of `plasmo-csui#jobright-helper-plugin`, where the
  page's `innerText` never looks. It shows "Autofilling" with three dots
  while their fill runs and "N/M required fields filled" once it stops,
  9/10 as often as 10/10. `STATUS_JS` walks shadow roots and reports
  `panel.busy` / `panel.done`; `run` finishes on done-after-busy, or a
  count untouched for `SETTLE_POLLS`; the field-count fallback never
  fires while busy. The `postMessage` protocol stays as the first word.
- **The trigger.** Two paths into the fill, both without a click. A job
  not yet in autopilot: the banner queues it, the pipeline tailors,
  `auto_approve` marks it approved and starts the fill. A job already
  in autopilot (approved, filled, or failed mid-fill): the injector
  reports every finished autofill to `POST /autofilled {url}`, and the
  server restarts the fill in that tab; a running fill is never doubled,
  the pipeline's own job is left to the pipeline, `submitted` and
  `skipped` are left alone. The banner stays purple from Add to the end
  of the fill; the injector's "autofill done" only moves the note.
- **Per-job auto-approve.** A box next to "Add to autopilot", checked
  by default, unticked during the countdown to make that job wait at
  checkpoint 1. Travels with `/capture`; on Jobright's posting page it
  goes ahead by `jr_id` (`POST /prefs`, in memory, used once) because
  the employer tab does the queueing.
- **Per-job model ("Use Opus").** A green button on the same banner,
  armed during the countdown, which does not cancel it: the job is
  queued as it would have been, with `Job.tailor_model` set to
  `llm.premium_model()`. The pipeline hands that name to `tailor.tailor`
  and `cover.write`, so the two documents a human reads are written with
  it and nothing else changes model. The resolved name is stored rather
  than a flag, so a folder still says what it was written with after the
  setting moves. It travels the same two ways auto-approve does
  (`/capture`, or `jr_id` through `POST /prefs`), and the review page's
  thread offers it after the fact ("Re-tailor with Opus"), which marks
  the row so the job stays an Opus job. That button needs no instruction;
  the other two in the thread are disabled until something is typed,
  because an empty box used to swallow the click.
- **The documents in code.** `forms.run_documents` on the reused tab
  once Jobright is done: remove the file on the resume slot (Greenhouse
  drops the input once a file is on it and shows "Remove file"; the
  label goes through the submit guard), set ours with
  `DOM.setFileInputFiles`, read the name back, then the cover letter
  where there is a slot, clearing an occupied one first. Two bugs on
  the way: the scan's ref counter restarted at 0 on every scan, so the
  returning input got the first-name field's ref and the upload hit a
  text input ("Node is not a file input element"); and `find_tab`
  matched by host and path, so "Fill again" on pallet ran three times
  on Amira's tab and left pallet's cover letter there. Refs now start
  above the highest on the page; tabs match on the whole URL minus
  visitor tags.
- **No agent.** `AUTOPILOT_AGENT=0` in `.env`: autofill, documents, a
  CDP screenshot, `form_fill.json`, stop, under a minute. `filled` iff
  the resume read back on the slot. The browser model (`qwen3.7-flash`
  in `.env`, ~50 s a step) had spent 30 steps on one dropdown; the form
  is the human's from here, and the agent is one flag away when wanted.
- **The browser closed under a fresh tab.** "I submitted it" on one job
  closed Chrome because the other job was `failed`; the human had just
  re-opened its form. `_in_progress` now also lists every other
  employer tab open in our Chrome.

### Status

Built, 504 tests. Live: the whole path on two Greenhouse embeds, end to
end, from Jobright's Apply to a green badge with the tailored resume and
cover letter on the form, no model. Not yet seen live: the box unticked,
a non-Greenhouse slot, `/autofilled` on a `failed` job with no resume.

## Phase 15 — projects from GitHub (built)

The resume names a project in one line; the interview asks ten questions
about each one on it and nothing about the forty that are not. Most of
what a project is can be read off its repository, so that is what the
Projects tab does (2026-09-20): a handle in, every public repository
read, ranked, the best ten ticked, and the candidate asked only what the
repository cannot say.

### Decisions

- **No git, no token.** The listing endpoint carries description,
  language, stars, dates and size; one API call per repository counts
  the handle's commits off the `Link` header's last page; the code is
  the tarball from codeload, which is not rate limited, read in memory
  (tree, README, manifests, the heads of five entry-point files, 45 KB
  to the model). Fifty repositories fit inside the unauthenticated hour.
  `AUTOPILOT_GITHUB_TOKEN` is there for more, never required. Public
  repositories only; private is deferred.
- **The scaffold is the cheap model's, the questions are the whole
  interview.** `github_rules.md` asks for what it does, how it is built,
  the stack from the manifests, what the commit log says about the
  candidate's part, and one to three questions the repository cannot
  answer (outcome, users, why, hardest). A README's claims stay "the
  README says". The scaffold becomes the project's `main.md` spine; the
  answers fill the rest; `tailor.md` and `star.md` follow as for any
  story. A GitHub project's checklist is its questions (`g1..gN`), and
  the code closes it after the last answer, no wrap-up.
- **Ranking is arithmetic.** Recency, stars, commits, a README, a
  description, size, each bounded, so an old project with real work
  still ranks (`github.score`). The top ten are ticked, plus any
  repository whose name is a resume project's; the candidate unticks
  and ticks before confirming. Not a model call: the list is the
  candidate's to change, and a model's order would be argued with.
- **A match on the resume is one story, not two.** The resume says
  "Project Hydra (Distributed Systems Platform)", GitHub says
  `Project-Hydra`; `match_resume_project` joins them by the repo name
  inside the title or a close ratio, the experience becomes "Resume
  name (repo-name)", and the scaffold is folded into its documents
  (rewritten by `update_main` and regenerated when already closed).
  Doubtful pairs are left apart and the candidate ticks the repo as a
  new project.
- **The link is the candidate's.** Every scaffold carries `Link:` (the
  repository by default); the Projects tab edits it, since a Chrome Web
  Store listing or a live site is often the better address, and the
  line is rewritten in `main.md` and `tailor.md` in code. That line is
  the only URL the tailor may put on the resume beyond the master's
  own: `rules.md` allows one whole `PROJECTS` entry to be swapped for a
  linked story, hyperlinked as the existing entries are, and
  `tailor._check_links` rejects any other `\href`.
- **Forks and empty repositories are issues, not judgements.** Listed
  under "Skipped" with why; a fork the candidate did the work in is
  theirs to add by hand. An owned repository whose commits carry an
  email GitHub has not linked comes back with zero attributed commits;
  it is counted whole and noted rather than skipped (two of the
  handle's own repositories on the first live run).

### Status

Built, 525 tests. Live: 54 repositories, 8 forks, 40 read in nine
minutes, scaffolds accurate on the ones checked (AudiTex: Kokoro, ONNX
Runtime Web, OPFS, three sane questions). Ranking put an auto-synced
problem-solutions repository first on commit count; the candidate
unticks it, and a weight against such names is the next step if it
recurs. Not yet seen live: confirm into a running interview, the short
interview itself, a swap on a tailored resume.

## Phase 16 — corrected fields, picked by hand, applied over autofill (built, 2026-09-20)

Phase 12 recorded corrections and nothing applied them: the code filler
that reads `answers` is off (`AUTOPILOT_FORM_FILL=0`, Jobright's
autofill is step one) and the agent is off (`AUTOPILOT_AGENT=0`). So a
location Jobright kept getting wrong was fixed by hand on every form and
learned every time, to no effect. And the learning itself was wrong:
every changed field at submit time went on file, silently, so a value
typed for one company would have become the value for every company.
"A very bad way for the AI to learn."

- **The human picks.** `corrections.changes` is the diff, rows
  `{label, was, now, remembered}`, and it is only ever offered: on every
  `/form-state` reply, so the banner on the filled form lists what
  changed since autofill (what autofill had struck through, what it is
  now) with a tick each and a **Remember** button, popping the bar open
  once when the first row appears; on the review page as "What you
  changed on the form", for after the tab is gone; on the confirmation
  page too, since the list survives the status change. `POST
  /review/{id}/remember {labels}` writes the ticked rows and nothing
  else. `/submitted` and `mark_seen` return the rows and write nothing.
- **The store is `corrections`, and a row is a record.** `value` (what
  the human set), `was` (what the form held, Jobright's value usually),
  `system`, `field` (the control's id, name, kind), `when`, `job`. The
  old `answers` key is read as records without a `was` and folded in on
  the next write. `forms.profile.corrections_of` is the one reader;
  `add_corrections` the one writer.
- **Applied where the wrong value comes from.** `Engine.apply_corrections`
  runs in `run_documents`, after the documents and before the open
  questions, on the same tab Jobright just filled: every field whose
  label matches a row, punctuation ignored, gets the corrected value
  over whatever autofill left. Never a textarea (prose is per job, the
  tailor writes it), never a visa question, no radio or option whose
  label reads as a submit. The logo says `corrected: … (autofill had …)`.
- **The snapshot carries identifiers and the page's text.**
  `forms.snapshot(detail=True)` returns the values, per label the
  control's id / name / kind, the visible text and the URL; the fill
  keeps the identifiers as `after_agent_meta`, `capture` as `meta`,
  `remember` puts them on the row, and `server/watch.py` reads the text
  to see a confirmation page.
- **The server watches the form itself.** Marking a job submitted from
  the page depended on the script being in the tab, re-evaluated after
  the navigation, with `sessionStorage` allowed; any miss left the job
  at `filled` after the person had pressed submit. `watch.py` attaches
  to every filled job's tab every 4 s: form there, keep its state; form
  gone and a confirmation phrase, mark it (`review.mark_seen`, shared
  with `/submitted-seen`). `/screen` answers a confirmation page in
  string work too, no model, and marks the job when it is ours. The
  page's own check wants the form gone before it believes a thank-you.
- **The page is a table, not a list.** Form details on the Profile tab:
  field, what the form filled (struck through), what you corrected,
  where (system, date), delete. Every row there is a value the next fill
  will write; nothing is applied that is not shown.
- **Adding a job never changes the active tab.** The review tab is
  pointed at the job in the background (`/__open` with `focus: false`,
  `Target.createTarget` `background`, the extension's `active: false`);
  the fill's own tab, on approve, is what comes to the front.
- **Bitten:** `Profile.__bool__` is the contact details, so a store of
  corrections alone is falsy and `corrections or Profile({})` dropped it
  silently (the test is on `None` now). Greenhouse removes the file
  input after an upload, so `MARK_FN` found nothing to put the logo on;
  the host is the tagged upload block now. The confirmation page was
  screened like a posting, with a model call, until `/screen` learned
  to see it first. And the whole thing ran for a day against a uvicorn
  started before the code existed; `pgrep -fl uvicorn` first, always.

Verified live so far: the fill's report carries `corrected` and the
identifiers, the pick list shows Delhi → Los Angeles on the Render job.
Not yet: a fill after a Remember, which is the point.

## Phase 17 — scout: a company's careers page, watched (built, 2026-09-20)

The pipeline starts from a posting someone found. For the few companies
where the person knows someone who can refer them, the finding is the
slow part: the role has to be seen the day it opens, and the referral
asked for before the posting fills. So: give it the careers page, have it
looked at a few times a day, be told the moment a role at the level
appears, with the message to the referrer ready.

Prior art was read first (`akv2011/job-watcher`, `ms-job-watcher`,
`job-alert-watcher`, `fulltime_newsletter`, the Apify scrapers). Every one
is thin wrappers on the same public endpoints plus a cron and a `seen`
file; none is licensed for lifting, none fits a one-click app (Node,
GitHub Actions, Streamlit, paid API), and this repo already owns the
parts they bolt on: the screen, the queue, the page, a background thread.
Reimplemented in ~500 lines.

### Decisions

- **The URL is the setup.** `scout.detect` reads host and path:
  Greenhouse (`boards.greenhouse.io/<slug>`, the embed's `?for=`),
  Lever, Ashby, Workday (`<tenant>.wd<n>.myworkdayjobs.com/<locale>/<site>`),
  SmartRecruiters, Oracle ORC (`/sites/<site>`), Eightfold (`?domain=`,
  and `careers.microsoft.com` mapped to its pcsx host), Google, Amazon,
  Apple. The page's own search filters carry over (`q`, `location`,
  `target_level`). An unknown host is refused with the list; a watch
  that can never list a role is not created.
- **Public JSON, no browser.** Each provider is one function over the
  endpoint the page itself calls: Greenhouse `boards-api`, Lever
  `api.lever.co`, Ashby `posting-api`, Workday `wday/cxs/.../jobs`
  (POST, `searchText`), SmartRecruiters `v1/companies/.../postings`,
  Oracle `recruitingCEJobRequisitions` with its `finder` string built by
  hand (URL-encoding breaks it), Eightfold `api/pcsx/search`, Amazon
  `search.json`. Google's `careers.google.com/api/v3/search` is gone
  (`Not Found`); the results page server-renders the rows into an
  `AF_initDataCallback` block, positional: id 0, title 1, apply URL 2,
  locations 9, description 10, posted epoch 12, team 15, preferred
  quals 18, minimum quals 19. The posting page by id is the hit's URL;
  row 2 is the sign-in-to-apply link. Apple is server-rendered HTML,
  titles from the link slugs. Microsoft answers ten rows a page whatever
  `num` says, so paging steps by what came back.
- **Level is a title filter, on the watch.** `filter.at_level`: one of
  the positives, none of the negatives, word-bounded substrings. The
  defaults are entry-level software; `III` is deliberately not negative
  (Google's new-grad level), `intern` is. Both lists are editable per
  watch on the page. The screen does the rest: every new title at the
  level goes through `server.screen.screen_url` with the description
  the listing carried (Google, Greenhouse, Lever, Ashby, Amazon send
  it; Workday, Apple, Eightfold do not, so the page is fetched), cached
  by URL, so a role later opened in the browser costs nothing twice.
- **Only what is new.** `seen_ids` on the watch. The first check of a
  page records everything listed and mails nothing: the point is what
  appears from then on, and today's list is on the page. Ids no longer
  listed are dropped, so a role that comes back after a gap is news
  again. At most `SCREEN_LIMIT` new roles are screened per check; the
  rest wait a round.
- **Frequency is one number.** `settings.scout_checks_per_day` (4), a
  per-watch override; `due` is 24 h / that since the last check; the
  thread ticks every minute and checks what is due, serially. "Check
  now" and "Verify" on the page run outside the schedule.
- **Broken is loud, three ways.** A fetch that raises or a page that
  lists nothing sets `Watch.error`: red `BROKEN` on the tab button from
  every view, "This company does not work" on the Scout tab and on the
  watch, and one mail (`broken_mailed`, reset when it reads again, so
  it never nags and does say it again on the next breakage). Adding a
  URL runs `verify` on the spot and says how many roles, how many at
  the level, the first titles.
- **Mail is SMTP from a personal account.** stdlib `smtplib`,
  `AUTOPILOT_SMTP_USER` / `_PASS` / `AUTOPILOT_MAIL_TO`, Gmail by default
  with an App Password, host and port for anything else. One digest per
  round with every hit not `reject`, the verdict and summary, the
  referrer and a note to forward. Not configured: the page says so, the
  hits stay on the page. Chosen over the Gmail API because four `.env`
  lines is what a friend will do and an OAuth consent screen is not.
- **The referral note is a template.** It goes to a friend and should
  read like the person, not like a model; company, title, first two
  locations, link, one line asking. Copy button on the hit.
- **Nothing is queued by itself.** A hit sits at `new` until "Add to
  autopilot" (`run.queue_hit`, the same three steps as `/capture`: queue
  row, saved posting text, pipeline start), "Dismiss", or a page
  breakage. The pipeline then runs as for any job, checkpoints intact.
- **`POST /settings` changes only the keys sent.** It took the whole
  model with defaults; the page's `{use_profile: true}` would have reset
  the scout frequency every load.

### Status

Built and verified live: ten providers probed the day of writing
(Google 101 rows / 30 at level unfiltered by location, Greenhouse 664 /
33, Ashby 148 / 18, Workday 300 / 39, Microsoft 300, Amazon 300 / 193,
Apple 100 / 11, Lever 313 / 126); a Google watch on early-career US
software engineers seeded with two roles; one forced re-discovery went
through the real screen (`ok`) and onto the page with the note; a
deliberately wrong SmartRecruiters company came up BROKEN on the tab
button, the banner and the row. Mail not yet configured on the
author's machine; the digest and the broken notice are tested with the
sender stubbed. `tools/scout_verify.py` exits with the broken count.
Mail configured 2026-09-21 (Gmail App Password), test mails received.
Not yet: a real hit arriving on the schedule.

## Phase 17b — scout feeds autopilot; notify watches for referrals (built, 2026-09-21)

Phase 17 listed every hit and waited for a click. In use, the click was
the same click every time for most companies, and the only companies
where the person wanted to be asked first were the ones where a referral
was possible. So the watch got a kind.

### Decisions

- **`Watch.notify`.** True: the company is one where the person can get a
  referral. Its hits are listed under the Scout tab's Notifications view
  and mailed with the link and the note for the referrer; the scout
  never queues them. False (default): the hit goes into autopilot from
  `run.check` the moment its verdict is in `run.QUEUE_VERDICTS` (`ok`,
  `caution`), through `queue_hit`, i.e. `/capture`'s three steps. The
  pipeline, checkpoint 1 (auto when `auto_fill`), the fill and the
  never-submit rule are unchanged; the scout only replaced the click.
- **A reject, or a screen that could not run, waits.** `verdict == ""`
  (no description, screen failure) is not a verdict; those stay on the
  Autopilot view with Add and Dismiss, as before.
- **Pre-screen in code before the model** (`filter.prescreen`). In bulk,
  most of what the title filter passes is still not entry level. Three
  cheap, high-precision reasons: `YEARS_MIN` (4) or more years of
  experience, taking the first number of a range so "0-2" and "2-4"
  pass and "1-3 … and 5+ years of Python" passes too; a security
  clearance; citizenship as a requirement. Everything softer is the
  model's. Wider `DEFAULT_POSITIVE`: data engineer, backend, full stack,
  frontend, platform, infrastructure, ML/AI engineer, SRE, DevOps, MTS,
  applied scientist, data scientist.
- **The add form is company and URL**, plus the notify box; the referrer
  field appears when it is ticked. Query, filters and frequency stay on
  the watch's page.
- **The digest says which is which.** Subject "N to ask about and M in
  autopilot at …"; the referral rows first, the queued rows marked "in
  autopilot"; rejects still never mailed.
- **A company's own page resolves to its boards** (same day). Mujin's
  `mujin-corp.com/careers` is a shell over a Lever board (Japan) and a
  BambooHR one (US). No provider for the host: `providers.discover`
  reads the page once, `BOARD_LINKS` finds every board link, and
  `/watch` makes one watch per board, "Mujin (lever)", "Mujin
  (bamboohr)". BambooHR provider added: `/careers/list` for the board,
  `/careers/<id>/detail` for the description of ids not yet seen.
- **The watch page is the board at the level, now** (same day). Hits are
  what appeared since the last look; the person also wanted to see what
  is there today. `Watch.listed` keeps the matching rows per check, the
  page lists them with Add; `add_listed` makes a hit and queues it.
- **US only in code** (`Watch.us_only`, `filter.outside_us`). A location
  that names another country, in every listed place, is out before the
  count; unknown places stay and the screen reads the posting. Mujin's
  Lever board went from 3 at level to 0, all Tokyo and Best.
- **Notifications first** in the sidebar; the watched list grows, the
  to-ask list is what needs the person.

## Phase 18 — the tailoring prompt, a method instead of a list of don'ts (2026-09-21)

The rules said what may not change and how long each line is; they did
not say how to decide what to change. Runs came back safe and flat. The
prompt now carries the method the 2026 guides, the XYZ-bullet advice and
the grounded-optimisation paper agree on: read the posting into a
ranked term list (title and level, required, preferred, responsibilities,
what repeats), in the posting's own spelling; map each term to the
resume line or profile story that proves it, or call it a gap; spend the
edits in order (summary in the posting's words, the two or three bullets
that prove the top terms with the term front-loaded and result, measure,
method when the pieces are already on the page, skills order and
spelling, entry order); check the reply against the checker's rules
before sending. Nothing the checker enforces changed. The rationale
opens with `Asks:`, the top five terms, so the reader sees what the
model read the posting as. Judged on the next real runs; the profile is
still empty, and the prompt already treats stories as evidence.

Tests: `tests/test_scout.py` (24): a non-notify watch queues `ok` and
`caution` only, a notify watch never queues, the digest orders and
labels, the pre-screen cases, and the pre-screen answering before the
model is reached. Verified on the page over CDP: both views render, the
tab badge is hidden when nothing is broken (it was shown always: the
badge's `display` beat the `hidden` attribute and its loader was never
written; both fixed the same day).
