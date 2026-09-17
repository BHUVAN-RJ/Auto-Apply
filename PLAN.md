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
- **Chrome stays the user's own.** The fill attaches over CDP; the
  extension needs Chrome anyway. That is the second click.
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
```

## Flow

```
you click Apply on Jobright        -> landing page screened, OK / NOT OK bar
OK or CAUTION queues itself in 2 s ->  queue.json, and pipeline.py starts
(NOT OK waits for the button; any page still queues from the context menu)
    fetch posting -> tailor -> compile -> cover letter
    CHECKPOINT 1   web page: rationale, diff, PDF, letter; approve, reject, or revise
    approve starts apply.py (AUTOPILOT_AUTOFILL=1)
    browser fills the form, replaces the resume, attaches the letter,
      answers open questions, screenshots, leaves the window open
    CHECKPOINT 2   web page: screenshot + answers; you submit by hand
```

Both checkpoints block. Nothing proceeds past checkpoint 1 without a click.

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
- Only the server closes the browser, and only on a submission that leaves no
  other job approved, filling, or filled. The agent never closes it.
- No file under `applications/` is ever deleted or overwritten. Reviewer
  decisions are the exception and append, since changing your mind is part of
  the record.
- The agent never closes a job. A poor-fit verdict is a recommendation that
  still stops at checkpoint 1; only the human sets `skipped`.
- A fill is only reported as filled if the agent actually finished and its
  own `upload_file` of the tailored resume succeeded, read from the action
  log. A screenshot proves the browser was alive; the model's claim that
  the resume is attached proves nothing.
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
   auto-reject, amber for a caution, green for clear, nothing if the page is
   not a posting. Each flag is one line: category, the posting's own words,
   why it applies. A dismiss button, and an "Add to autopilot" button that
   calls the existing `/capture`.
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
| `timeline` | Enrolment after graduation; a start date or term the facts rule out | A window the latest degree falls in; a cohort year in the title alone |
| `location` | Outside the applicant's country; remote restricted to a region they will not move to | Another city in the same country (relocation is the default); several offices listed; HQ in a remote header |
| `degree` | PhD or a licence required, no equivalent | "CS or related" when held; "MS preferred" when held |
| `seniority` | Staff, principal, director, manager with scope stated | Level names (II, L4); a company's own levelling vocabulary |
| `perm` | Two or more PERM tells: mail-in resume, job code, single exact salary, "labor certification", a named HR contact | A plain salary range |
| `other` | A human language; a physical requirement; "internal only" | Skills, domains, salary, posting age |

The verdict is `reject`, `caution`, `ok`, or `not_a_job`, recomputed from
the flags in code. Derived facts now carry a relocation line so the model
does not have to guess.

### Where things go

| Path | What |
|---|---|
| `base/applicant.md` | The applicant's hard facts, one `## Facts` section the screen prompt reads verbatim. Gitignored; `base/applicant.example.md` is the template. Already read by `browser/fill.py` for form details. Phase 7 grows this file |
| `tailor/screen.py` | `screen(posting_text, applicant) -> Screen`; JSON reply parsed and validated against the category enum, never trusted raw |
| `tailor/screen_rules.md` | The prompt, sent verbatim, same convention as the other rules files |
| `server/screen.py` | `POST /screen` and the URL-keyed cache in `data/screens.json` |
| `capture/content.js` | Text extraction, banner, posts to the server. Runs on `jobright.ai/jobs/info/*` (verdict only) and, via `background.js`, in any tab opened from or navigated away from Jobright, wherever Apply lands (verdict, then a two-second countdown into `/capture` for `ok` and `caution`) |
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
