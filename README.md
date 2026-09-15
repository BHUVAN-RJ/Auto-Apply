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
| `capture/` | MV3 Chrome extension. Screens every job page as it opens and paints the verdict; right-click any posting → "Add this job to autopilot" |
| `server/` | FastAPI on `localhost:8787`. Owns the queue, serves the review UI, holds checkpoint state |
| `tailor/` | Reads the posting plus your profile, edits the resume, emits a diff and a rationale |
| `tex/` | `lualatex` wrapper producing deterministic PDFs |
| `browser/` | `browser-use` fill loop driving your real Chrome profile |
| `review/` | Local web page: diff view, PDF preview, approve / reject / chat |
| `tools/sweep_failed.py` | Moves failed application folders under `applications/failed/`; nothing is deleted |
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

The full path works end to end: capture, scrape, tailor, compile, archive,
checkpoint 1, browser fill, checkpoint 2. The on-page screen and the profile
switch are built on top. Next is the profile interviewer (PLAN.md, Phase 7),
then hardening — retries, resume-after-crash, a per-ATS recipe cache. See
[PLAN.md](PLAN.md) for the design and what is deliberately deferred.

```sh
python pipeline.py            # tailor and compile every queued job
python apply.py               # fill every approved form, then stop
                              # (approving in the UI starts this for you)
pytest                        # 307 tests
```

## Setup

```sh
brew install --cask basictex        # needs sudo
uv venv && uv pip install -e .
cp .env.example .env                # add your OpenRouter key
cp base/applicant.example.md base/applicant.md   # fill in the Facts section
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

## Checkpoint 1

`localhost:8787` groups the sidebar by what needs doing — needs your review,
filled and awaiting your check, approved, queued, submitted, rejected, failed —
and opens on whatever is waiting. Selecting a job shows its rationale, a
coloured diff against your master resume, the compiled PDF inline, the filled
form screenshot once there is one, the scraped posting, and the full status
history.

Three ways out:

- **Approve** — the only path onward. The fill loop refuses any job that is not
  approved, so this gate cannot be skipped.
- **Reject** — the job is dropped. The folder stays as a record of what was
  tried and why it was not sent.
- **Re-tailor with a note** — a second pass with your instruction appended to
  the prompt, landing in a new folder. The version you rejected is kept.

## Every decision is yours

The agent never closes a job. It can recommend: a poor-fit verdict flags the
posting, explains why, and tailors nothing — but the job still waits at
checkpoint 1 for you to reject it or push back with a re-tailor note. The only
states the agent sets are working states and `filled`. Approving, rejecting,
and marking submitted are reachable only from the review page.

Rejecting requires a reason — one of seven, plus an optional note — because a
rejection with no reason tells you nothing three weeks later. Rejected jobs
stay in the list with the reason under them, and the folder is kept intact.
You can reject at any stage, including after approving or filling.

## Checkpoint 2

Approving starts the fill (`AUTOPILOT_AUTOFILL=1`, the default in `.env`;
unset it to start fills by hand with "Fill the form now" or `python
apply.py`). For the first 30 seconds the page shows only "Agent is working"
with no buttons. After that "Fill it again" and "Reject…" come back.

One fill per job at a time. Two `apply.py` runs on the same job drive the
same Chrome tab and undo each other's steps, so the server refuses a second
one while the first is alive. "Fill it again" is double-gated: the server
answers `fill_running`, the page asks "This will kill the current job and
restart. Are you sure?", and only a yes kills the running fill and starts a
fresh one. A run whose browser died shows "The fill died" and restarts
without the question.

Capturing a job starts the pipeline on its own: by the time the review tab
is open the posting is scraped and the resume and letter are ready, or a few
seconds away. A job captured while the server was down shows a "Tailor it
now" button instead.

The file is uploaded as `<AUTOPILOT_RESUME_FILENAME>.pdf` (spaces become
underscores; default `Resume.pdf`). Set it in `.env` to whatever you want the
recruiter to see. A cover letter is written alongside the resume, from the
tailored resume and `tailor/cover_rules.md`, shown on the review page as text
and PDF, and uploaded as `<AUTOPILOT_COVER_LETTER_FILENAME>.pdf` wherever the
form offers a cover letter upload. A form with only a text box gets nothing
typed into it. A letter that fails to generate does not fail the job; the
error shows on the review page and re-tailoring tries again.

Open questions on the form ("Why do you want to work here?") are answered by
the tailoring model from the posting, the tailored resume, your profile, and
the cover letter, in the same short, plain style; the browser agent only types
them. Every answer is saved to `answers.md` and shown on the review page.

Questions about visa, sponsorship, work authorisation, OPT / CPT, citizenship
or immigration status are never answered by the agent: any attempt to type in
or pick from such a field is refused in code. If Jobright's autofill has set
one from your profile it stays; otherwise it is left blank for you.

The fill runs detached, so a browser crash cannot take the server down. Its log
lands in `data/apply_<job>.log`, and the review page shows the tail of it under
"Fill log" along with the failure reason, so a broken run can be diagnosed
without leaving the page.

The browser model must accept images — browser-use sends a screenshot to the
model on every step, and a text-only model returns 404 on all of them and fills
nothing. `OPENROUTER_BROWSER_MODEL` defaults to `google/gemini-3.1-flash-lite` for that
reason; vision is switched on only for a model whose name says it can take
images, and `AUTOPILOT_VISION=0/1` forces the decision either way.

Reliability at the action schema matters more than price here. `glm-5v-turbo`
accepts images but emits `{"click": [3713]}` where browser-use requires
`{"index": 3713}`, so every step fails validation and the run explores until it
exhausts its step budget.

A fill runs 20-40 steps with a screenshot on each, so the per-step price
compounds; `google/gemini-3.8-flash` is the fallback if a cheaper model's fills
come out wrong. Watch one as it happens with `tail -f data/apply_<job>.log`.

Either way it opens a browser, fills the form, and halts — leaving the window
open on the completed form. That is the point of checkpoint 2: you read the
form in the browser, submit it yourself, and press "I submitted it". The
agent's session ends without touching the browser process.

The window is shared between fills, so closing it is the server's call, not
the agent's. Marking a job submitted closes the Auto-Apply Chrome (CDP
`Browser.close`, the same as Quit, so logins are saved) only when no other
job is approved, filling, or filled and waiting for its own check. Otherwise
the page says "Application filling in progress" and lists what is holding
the window. "I submitted it" is also available while a job is still
`filling`: your submission wins, and the agent still poking at the form is
killed.

### The browser profile

Chrome refuses to share a user-data-dir with a running instance and silently
falls back to a throwaway, so pointing at your everyday profile loses every
login on each run. Two ways round that:

**A profile of its own** (default). Auto-Apply keeps a real profile directory
at `~/Library/Application Support/job-autopilot/chrome`. Set it up once:

```sh
python tools/open_profile.py
```

Sign in to Jobright in the window that opens, install its extension, close it.
The login persists for every later run, and it never touches your day-to-day
browser.

**Attach to a browser you already have open**, with all its existing logins and
extensions. Quit the browser fully, then relaunch it with a debugging port and
point Auto-Apply at it:

```sh
python tools/open_profile.py --attach   # prints the exact commands
export AUTOPILOT_CDP_URL=http://127.0.0.1:9222
```

This works with Brave, or any Chromium browser. The flag only applies to a
fresh launch, so the browser has to be fully quit first. It refuses any job that is not approved, so checkpoint 1 cannot be
bypassed by running it directly.

Three independent things stop it submitting, none of them a prompt rule:

- **The click guard.** Every click is checked first against the element's text,
  accessible name, id, name, and value. Anything reading as submit, apply,
  send, finish, or confirm-and-send is refused. Attribute values are split on
  underscores and hyphens, so `submit_application` is caught as readily as
  "Submit application". Navigation controls — save and continue, next, search,
  upload — pass through.
- **A reduced vocabulary.** `evaluate` and `send_keys` are removed from the
  agent entirely. Arbitrary JavaScript would make every other guard
  decorative, and Enter submits a single-input form with no button click.
- **A terminal state.** The run always ends in a screenshot and `status:
  filled`. `SUBMITTED` is reachable only from the review page, only on a job
  that is `filled` or still `filling`, and only behind a confirmation.

You then review the screenshot, submit in the browser yourself, and mark it.

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

## On-page screen

Click Apply on Jobright or LinkedIn, and whatever site it lands on gets a
bar across the top within a couple of seconds: red NOT OK for an
auto-reject, amber CAUTION for a soft one, green OK for clear, grey when
the page holds no posting (a login wall, a redirect still loading; press
Again). The same happens on the usual ATS domains opened any other way. Each line is one requirement in the
posting's own words — years of experience, no sponsorship, ITAR, clearance
or citizenship, a start date or graduation window, a role outside the US,
a required degree, a senior title — and why it applies to the facts in
`base/applicant.md`. Without that file the facts are derived from the resume
once and the bar says so, since a resume knows nothing about visas or start
dates. "Add to autopilot" on the bar is the same capture as the context
menu. The verdict is cached per URL, reused by the pipeline,
and shown again on the review page. It is advice; nothing is skipped by it.

## Use profile

A switch in the page header. On, the tailor, the cover letter, the form
answers, and the screen are given `base/applicant.md` and every document in
`base/stories/` (one per role or project; the interviewer that writes them
is the next phase). Off, or on with nothing written yet, everything runs on
the resume and `base/profile.md` alone.

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
defaults to `z-ai/glm-5.3`; anything OpenRouter serves works. No cloud services beyond the model
API; the queue, the archive, and the review UI are all local.
