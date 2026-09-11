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
| Typesetting | Local BasicTeX via `lualatex` | Removes the Overleaf round trip; deterministic, scriptable, offline |
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
- The fill loop's terminal action is always a screenshot plus `status: filled`.
- No file under `applications/` is ever deleted or overwritten.

## Phases

| # | Work | Estimate |
|---|---|---|
| 0 | BasicTeX install, compile `base/resume.tex` end to end | 0.5 day |
| 1 | Capture extension plus queue server | 0.5 day |
| 2 | Tailor, compile, archive, diff | 1.5 days |
| 3 | Review web page, checkpoint 1 | 1 day |
| 4 | Browser fill loop, checkpoint 2 | 2-3 days |
| 5 | Glue, `start` command, index, retries | 0.5 day |

Phases 0 to 4 are built. Phase 5 is partly done: `pipeline.py` and `apply.py`
are the entry points, and `index.csv` is written, but retries and
resume-after-crash are not.

## Deferred — noted, not built

- **Recipe cache per ATS domain.** Log a successful action sequence on first
  encounter with a domain, replay it as a deterministic script on later jobs,
  falling back to the agent loop when replay fails. Only worth building once
  fill quality is proven.
- **Queue file-watcher auto-trigger.** For v1, `start` is run by hand.
- **Glasses as the checkpoint surface.** The long-term goal is for both
  checkpoints to render on glasses: the resume diff appears, it is confirmed
  or rejected there, and the pipeline continues without touching a keyboard.
  This is why checkpoint 1 is a web page rather than a terminal prompt. No
  glasses-specific work is in scope for v1.
