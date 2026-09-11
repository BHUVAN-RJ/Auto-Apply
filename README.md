# Auto-Apply

A semi-autonomous job application pipeline. It takes a job posting you flagged,
tailors your LaTeX resume to it, fills the application form, and then stops —
every submission is a human click.

The point is not to apply to jobs while you sleep. It is to remove the twenty
minutes of copying, pasting, re-compiling, and re-uploading that sits between
"this job looks good" and a submitted application, while keeping you in the
loop at the two moments that actually matter.

## The workflow it replaces

Done by hand, applying to one job looks like this:

1. Find a posting worth applying to and open the application page.
2. Copy the description into an LLM along with your profile, and read back
   suggestions for how the resume should change.
3. Paste in the resume LaTeX, get the edits, compile, download the PDF.
4. Click Apply. A browser autofill extension populates the form. Swap in the
   freshly tailored PDF, check the fields by eye, submit.

Auto-Apply performs steps 1 through 4 up to — but never including — the submit
click, pausing twice for your approval.

## Two checkpoints, every application

**Checkpoint 1 — the resume.** After tailoring, a local web page shows a
unified diff against your master resume, a preview of the compiled PDF, and the
model's written rationale for each change. Approve it, reject it, or chat to
revise it.

**Checkpoint 2 — the form.** After the browser fills the application, you get a
screenshot of the completed form. The agent halts there.

Both checkpoints block. Nothing advances without an explicit click.

## The agent never submits

This is enforced in the action layer, not in a prompt:

- The browser agent may not click any element matching a submit-button pattern.
- The fill loop's terminal action is always a screenshot plus `status: filled`.
- No file under `applications/` is ever deleted or overwritten.

Prompt-level rules leak under pressure. A deny-list in code does not.

## Everything is kept

Every application gets its own immutable folder:

```
applications/2026-09-10_stripe_backend-engineer_a3f1/
  job.json             url, source, title, company, scraped_at
  posting.md           full description snapshot
  resume.tex           tailored source
  resume.pdf           compiled artifact
  resume.diff          unified diff against base/resume.tex
  suggestions.md       rationale for each change
  fill_screenshot.png  completed form, pre-submit
  status.json          filled | confirmed | submitted | skipped
```

Re-tailoring the same job creates a `_v2` folder rather than overwriting.
`applications/` is gitignored — it holds your full employment history and every
job you ever considered.

## Architecture

| Component | Role |
|---|---|
| `capture/` | MV3 Chrome extension. Right-click any posting → "Add this job to autopilot" |
| `server/` | FastAPI on `localhost:8787`. Owns the queue, serves the review UI, holds checkpoint state |
| `tailor/` | Reads the posting plus your profile, edits the resume, emits a diff and a rationale |
| `tex/` | `lualatex` wrapper producing deterministic PDFs |
| `browser/` | `browser-use` fill loop driving your real Chrome profile |
| `review/` | Local web page: diff view, PDF preview, approve / reject / chat |
| `archive/` | Application folder writer plus `index.csv` |

The queue file is the seam between the capture half and the pipeline half,
which keeps the two independently debuggable.

Browser control runs against your **real Chrome profile**, not a fresh
automation profile, so existing logins and an already-installed autofill
extension are available to the agent. Reusing an autofill extension that
already knows the major ATS field layouts is deliberate — reimplementing
per-ATS field mapping is the largest time sink in a project like this and buys
nothing.

## Status

Early. Phases 1 and 2 of 6 are done: a posting can be captured, scraped,
tailored, compiled, and archived, and it stops at checkpoint 1. The review UI
currently lists the queue but does not yet render the diff, and the browser
fill loop is not built. See [PLAN.md](PLAN.md) for the full design and phase
breakdown.

```sh
python pipeline.py            # process every queued job
python pipeline.py <job_id>   # process one
pytest                        # 50 tests
```

## Setup

```sh
brew install --cask basictex        # needs sudo
uv venv && uv pip install -e .
cp .env.example .env                # add your OpenRouter key
```

Load the capture extension: Chrome → `chrome://extensions` → Developer mode →
Load unpacked → select `capture/`.

## Run

```sh
.venv/bin/python -m uvicorn server.app:app --host 127.0.0.1 --port 8787
```

Open <http://127.0.0.1:8787> for the queue. Right-click any job posting in
Chrome and choose "Add this job to autopilot" to queue it, then run
`python pipeline.py` to tailor and compile everything queued.

Put your master resume at `base/resume.tex`, along with any `.cls` or `.sty`
it needs. `base/resume.example.tex` shows the shape and is what the test suite
compiles. Optionally add `base/profile.md` with background that is not on the
resume; the tailor reads it as extra context but is instructed never to invent
anything it cannot support.

## Tailoring rules

What the model may and may not change lives in
[`tailor/rules.md`](tailor/rules.md), which is sent to it verbatim. Edit that
file to change tailoring behaviour; no Python changes are needed.

The defaults are conservative. Only `SUMMARY`, `EXPERIENCE` bullets, `PROJECTS`
descriptions, and `TECHNICAL SKILLS` may change. The preamble, heading,
education, achievements, company names, job titles, dates, and project URLs are
frozen. Nothing may be invented: every edit has to be traceable to something
already on the page.

Three of those rules are enforced in code rather than trusted to the prompt,
because prompt rules leak:

- **Frozen sections.** The tailored source is parsed into sections and every
  one outside the editable set must come back unchanged, ignoring whitespace.
- **Bullet count.** The layout is tuned for exactly the bullets that are there,
  so adding or dropping one is rejected.
- **Page count.** The candidate is compiled before it is accepted. If it does
  not match the master's page count, the model is told what it did and asked
  again, up to three attempts.

A rejection is fed back to the model with the specific problem named, so the
retry is informed rather than a reroll.

## Fit assessment

The model judges fit before it tailors. A clear mismatch — wrong domain, a
required credential the candidate lacks, an impossible location — returns a
`MISMATCH` verdict, and the job is marked skipped with the reason in
`mismatch.md`. Nothing is tailored and no tokens are spent on a resume that
will not be sent. A missing nice-to-have is not a mismatch.

## Missing TeX packages

BasicTeX ships a minimal package set, so a resume template that pulls in
anything beyond the basics fails to compile. The error names the fix:

```
missing TeX package(s) enumitem. BasicTeX is minimal;
install them with: sudo tlmgr install enumitem
```

Run that and compile again. `base/resume.example.tex` deliberately avoids
every non-default package so the test suite passes on a bare install.

## Stack

Python 3.10+, FastAPI, [browser-use](https://github.com/browser-use/browser-use),
OpenRouter, and a local TeX Live install. The model is configured in `.env` and
defaults to `z-ai/glm-5.1`; anything OpenRouter serves works. No cloud services beyond the model
API; the queue, the archive, and the review UI are all local.
