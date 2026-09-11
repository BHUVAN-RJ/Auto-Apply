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

## Architecture

```
capture/   MV3 Chrome extension. Context-menu "Add this job" on any page.
           Posts {url, title, source, added_at} to the local server.

server/    FastAPI on localhost:8787. Owns queue.json. Serves the review UI.
           Holds approve/reject/revise state for both checkpoints.

tailor/    Reads the posting plus the candidate profile, edits base/resume.tex,
           emits a unified diff and a written rationale per change.

compile/   lualatex wrapper. Deterministic output into the application folder.

browser/   browser-use fill loop. Opens the application URL, clicks Jobright
           autofill, swaps the resume file, screenshots, halts.

review/    Localhost web page. Diff view, PDF preview, Approve / Reject / Chat.

archive/   Application folder writer plus index.csv.
```

## Flow

```
you right-click "Add this job"     ->  queue.json
you run `start`
  for each queued job:
    fetch posting -> tailor -> compile
    CHECKPOINT 1   web page: diff + PDF + chat; approve, reject, or revise
    browser fills the form, swaps the resume, screenshots
    CHECKPOINT 2   web page: screenshot; you submit by hand
```

Both checkpoints block. Nothing proceeds without an explicit click.

## Archive layout

```
applications/
  2026-09-10_stripe_backend-engineer_a3f1/
    job.json             url, source, title, company, scraped_at
    posting.md           full description snapshot
    resume.tex           tailored source
    resume.pdf           compiled artifact
    resume.diff          unified diff against base/resume.tex
    suggestions.md       rationale for each change
    fill_screenshot.png  completed form, pre-submit
    status.json          filled | confirmed | submitted | skipped
base/
  resume.tex             master resume, git-tracked
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
  `submitted` is reachable only from the review page, only on a filled job.
- No file under `applications/` is ever deleted or overwritten. Reviewer
  decisions are the exception and append, since changing your mind is part of
  the record.
- The agent never closes a job. A poor-fit verdict is a recommendation that
  still stops at checkpoint 1; only the human sets `skipped`.
- A fill is only reported as filled if the agent actually finished. A
  screenshot proves the browser was alive, not that the form was filled.

## Phases

| # | Work | Estimate |
|---|---|---|
| 0 | BasicTeX install, compile `base/resume.tex` end to end | 0.5 day |
| 1 | Capture extension plus queue server | 0.5 day |
| 2 | Tailor, compile, archive, diff | 1.5 days |
| 3 | Review web page, checkpoint 1 | 1 day |
| 4 | Browser fill loop, checkpoint 2 | 2-3 days |
| 5 | Glue, `start` command, index, retries | 0.5 day |

Phases 0 to 4 are built and have run end to end against a real posting.
Phase 5 is partly done: `pipeline.py` and `apply.py` are the entry points,
`index.csv` is written, and the tailoring loop retries with specific feedback,
but resume-after-crash is not built.

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
  the screenshot and nothing is filled.
- **Reporting success on weak evidence is worse than failing.** That failed run
  was recorded as `filled` because a screenshot file existed, which sent the
  reviewer to inspect a screenshot of nothing and left no diagnosable trace.
  Rejected tailoring candidates and the fill log are now kept and surfaced on
  the review page.

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
- **Queue file-watcher auto-trigger.** For v1, `pipeline.py` and `apply.py` are
  run by hand or from the review page.
- **Auto-filling on approve.** Built, then reverted. Approval launching the
  browser meant a run whose browser died left the job in `filling` with no way
  to start another from the page, and the only visible symptom was a page that
  looked stuck. Filling is explicit and restartable from any post-approval
  state; `AUTOPILOT_AUTOFILL=1` opts back in.
- **Resume-after-crash.** A fill interrupted halfway leaves the form partly
  filled and the job restartable, but nothing reconstructs where it got to.
- **Cheaper vision for the fill loop.** `z-ai/glm-5v-turbo` is $1.20/$4.00 per
  million against `z-ai/glm-4.6v` at $0.30/$0.90. Worth measuring once there
  are enough real fills to compare quality.
- **Glasses as the checkpoint surface.** The long-term goal is for both
  checkpoints to render on glasses: the resume diff appears, it is confirmed
  or rejected there, and the pipeline continues without touching a keyboard.
  This is why checkpoint 1 is a web page rather than a terminal prompt. No
  glasses-specific work is in scope for v1.
