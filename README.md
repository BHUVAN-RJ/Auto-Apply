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
revise it. By default this checkpoint passes itself: the banner on the job
page has an **auto-approve** box, ticked, next to "Add to autopilot"; leave
it and a clean screen plus a clean tailor go straight on to the form. Untick
it during the two-second countdown and that job waits here for you. A
poor-fit verdict or a `reject` screen always waits.

**Checkpoint 2 — the form.** After the form is filled, you get a screenshot
of it and the window left open on it. Everything halts there.

Checkpoint 2 always blocks. Nothing is ever submitted by anything but you.

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
| `capture/` | `content.js`: the on-page banner (screen verdict, countdown, queue, already-seen, file chips, submission watch). Injected by `browser/inject.py` into the app's own Chrome; also loadable as an MV3 extension for an everyday Chrome |
| `server/` | FastAPI on `localhost:8787` by default. Owns the queue, serves the review UI, holds checkpoint state |
| `tailor/` | Reads the posting plus your profile, edits the resume, emits a diff and a rationale |
| `tex/` | `lualatex` wrapper producing deterministic PDFs |
| `browser/` | `browser-use` fill loop driving your real Chrome profile |
| `review/` | Local web page: diff view, PDF preview, approve / reject / chat, the Profile tab, and the Projects tab |
| `voice/` | Local speech for the interviewer: whisper.cpp in, Piper out, both as subprocess binaries |
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
switch are built on top. The Profile tab holds the interviewer: it reads
your resume, asks about each role and project the way an interviewer would,
and writes one story per experience that the tailor, the cover letter, and
the form answers draw on, plus a STAR write-up for you. The Projects tab
reads your public GitHub repositories, ranks them, and hands the ones you
tick to the same interviewer with one to three questions each, only what
the repository cannot say; the link on each is what a tailored resume
hyperlinks. Type, or switch to
voice mode and talk to the orb hands-free; speech runs on this machine
(whisper.cpp in, Kokoro out) and the models download on first use. Ashby,
Greenhouse and Lever forms are filled by code from the preliminary
interview's answers, and what you correct before submitting is learned for
the next form. Next: the same code fill for Workday, Oracle, iCIMS and
SmartRecruiters, where Jobright's autofill is still step one. See
[PLAN.md](PLAN.md) for the design and what is deliberately deferred.

```sh
python pipeline.py            # tailor and compile every queued job
python apply.py               # fill every approved form, then stop
                              # (approving in the UI starts this for you)
pytest                        # 525 tests
```

## Setup

```sh
brew install --cask basictex        # needs sudo
uv venv && uv pip install -e .
cp .env.example .env                # add your OpenRouter key
```

`base/applicant.md` (visa status, relocation, start date, the facts the
screen and the fill need) is written by the Profile tab's first
interview, eight spoken questions; `base/applicant.example.md` shows the
shape if you would rather type it.

## Run

Two processes, one browser:

```sh
.venv/bin/python -m uvicorn server.app:app --host 127.0.0.1 --port 8787
.venv/bin/python -m browser.inject --open https://jobright.ai/jobs/recommend
```

If 8787 is occupied, start uvicorn on another port and give the injector the
same origin, for example `AUTOPILOT_SERVER_URL=http://127.0.0.1:8788`. The
injector rewrites the injected banner's local-server URL to match; the unpacked
extension keeps its manifest-pinned 8787 origin.

The second launches (or reuses) the app's own Chrome on port 9333, with
your Jobright login and Jobright's extension in its profile, and puts the
screen into every Jobright tab and every tab Jobright's Apply opens. It
reattaches by itself if the connection drops and exits when Chrome quits.
No extension of ours is installed; to use your everyday Chrome instead,
load `capture/` as an unpacked extension (`chrome://extensions` →
Developer mode → Load unpacked).

Open <http://127.0.0.1:8787> for the queue. In the Chrome that opened,
browse Jobright: a posting page is screened as it opens; an OK or caution
verdict counts down and presses Apply; the employer's page is screened
again and queues itself; the pipeline runs; the job appears on the review
page. A posting already in autopilot is not queued again: the banner says
where it stands and opens it on the review page. Add, Add anyway, and Open in
autopilot focus the existing Autopilot tab and select that job; if there is no
Autopilot tab, exactly one is opened.

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
  approved. With auto-approve (the banner's box, or `auto_fill` in
  `data/settings.json`) the pipeline presses this for you on a clean job;
  a poor fit or a rejecting screen still lands here.
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
apply.py`). The fill works in the tab Jobright's Apply opened, where the
injector has already pressed Jobright's Autofill and waited for its panel
to stop saying "Autofilling". Then, in code and without a model, it removes
the resume Jobright attached, puts the tailored one on the slot, and the
cover letter where there is one, reading each name back off the form.

With `AUTOPILOT_AGENT=0` (the current setting in `.env`) that is the whole
fill: a screenshot, and the form is yours to finish and submit; the job is
`filled` only if the resume read back, `failed` otherwise. With
`AUTOPILOT_AGENT=1` the browser agent takes over from there for the
location and the open questions. For the first 30 seconds the page shows only "Agent is working"
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

Before the agent starts, the code fills what it can. On Ashby, Greenhouse
and Lever the form is filled without a model at all (`browser/forms/`):
every field is read with its label, matched to `base/form.json`, set the
way the widget expects (typed keys and a click on the suggestion for a
location picker, `DOM.setFileInputFiles` for the resume, read back to be
sure), and the agent gets the list of what is still empty. Elsewhere the
code presses Jobright's Autofill and waits for it to finish: Jobright's
own "done" message, or its panel going from "Autofilling ···" to "N/M
required fields filled", whatever N is (`browser/autofill.py`;
`AUTOPILOT_AUTOFILL_BY_CODE=0` leaves it to the agent,
`AUTOPILOT_FORM_FILL=0` skips the code fill, and that is the current
setting: Jobright first, always). The press does not wait for the
pipeline: the injector makes it the moment the employer's tab has loaded,
before the job is even queued, and the fill later works in that same tab.
Once Jobright is done, the documents go on in code: Jobright's resume is
removed from its slot, the tailored one set, the cover letter where there
is a slot (`AUTOPILOT_DOCS_BY_CODE=0` leaves that to the agent). From Add
until the fill ends, the corner badge's core stays purple and pulses,
with a line in the bar saying what is happening; green when the documents
are on, red when the resume did not attach. With `AUTOPILOT_AGENT=1` the
agent then works in that tab with per-system notes for Greenhouse, Ashby,
Workday, Oracle, and Lever (`browser/ats_rules.md`).

`base/form.json` comes from the **preliminary interview** on the Profile
tab: the fixed questions every application asks (contact, location, work,
education, self-identification, work authorisation), one per screen, no
voice, contact details prefilled from the resume. "How did you hear about
us" is always "Other". The authorisation answers are kept for you and the
screen; the filler never types them onto a form, that stays yours.

**What you correct, you choose to keep.** The form as autofill left it
is kept; while you check it the server reads the form back, and the
banner on the page lists what you changed — field, what autofill had,
what it is now — with a tick each. Tick the ones Jobright got wrong and
press Remember: those go into `base/form.json` under `corrections`,
keyed by the question as the form showed it, with the old value, the
system and the control, and on every next form that asks the same
question your value goes over whatever autofill put there (its location
is wrong most of the time; fix it once). Nothing is remembered without
the tick; the same card is on the review page for after the tab is
gone. The Form details page on the Profile tab shows the whole table
and a delete on each row. Free-form answers are written per job and
never carried over; visa questions are never offered.

If you would rather do it yourself, or the agent is slow: the banner on
the form's page shows the tailored resume and cover letter as chips. Drag
one onto the form's upload slot, click it to download, or press "put" to
set the matching file input directly.

When you submit, the page that follows is read by the same banner and the
job is marked submitted on its own; "I submitted it" still works and is
what closes that form's tab. The browser stays.

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

The window is shared between fills and is never closed by the app. Marking
a job submitted closes that job's form tab only; the Jobright list, the
next form and the logins stay. "I submitted it" is also available while a job is still
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

Use Google Chrome for this workflow. `AUTOPILOT_BROWSER` can pin its executable
when more than one Chromium browser is installed. The flag only applies to a
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

Open a posting on Jobright, or click its Apply button, and the page it
lands on gets a bar across the top within a couple of seconds: red NOT OK
for an auto-reject, purple CAUTION for a soft one, green OK for clear, grey
when the page holds no posting (a login wall, a redirect still loading).
Each line is one requirement in the posting's own words — years of
experience, no sponsorship, ITAR, clearance or citizenship, a start date or
graduation window, a role outside the country, a required degree, a senior
title, a PERM advertisement — and why it applies to the facts in
`base/applicant.md`. Without that file the facts are derived from the
resume once and the bar says so, since a resume knows nothing about visas
or start dates. Location anywhere in the United States is always green.
A graduation requirement phrased as a latest-date cutoff is also green when
the applicant graduates earlier (December 2026 satisfies "earned or expected
by Summer 2027"). Those two rules are enforced after the model response, not
left to prompt wording.

Off Jobright, an OK or CAUTION verdict queues the job by itself after a
two-second countdown drawn across the button; a click during it cancels.
NOT OK waits for the click. On Jobright's own posting pages the bar only
advises, because the Apply button leads to the employer's page, and that is
the URL worth queueing. The verdict is cached per URL, reused by the
pipeline, and shown again on the review page. It is advice; nothing is
skipped by it. A bare ATS shell or corporate footer reuses the cached
Jobright verdict; if no verdict exists yet, the saved Jobright description
and employer text are screened together in one model call. Jobright's visible
"Original Job Post" header supplies the role and company when its document
title is the generic recommendations title. Any other page still queues by
hand from the context menu.

Five seconds after a successful add, the bar minimizes to the assistant-circle
badge in the top-left corner. The badge stays for the life of that page and is
coloured for the cached verdict; clicking it restores the same banner without
screening again or spending another model call. Failed and no-posting screens
still offer both Again and Add.

A posting already in autopilot is never queued again. Provably the same
posting (same URL, same Jobright id, same job id at the tracking system)
is refused by the server; the same employer and role or description is a
"looks like" line with "Add anyway". Either way the bar shows the job's
status and an "Open in autopilot" button; the countdown does not run.
Successful Add and Add anyway actions carry the captured job id into that same
tab-routing path, so they always leave the matching Autopilot job visible.

## Form details

`base/form.json` (gitignored; template `base/form.example.json`) is what
the code filler types: one key per field, and `corrections`, the question
label to what you set it to, grown from what you fix before submitting. Fill it in from the
Profile tab ("Preliminary interview" / "Form details") or by hand. Without
it the code fill stands aside and Jobright's Autofill is pressed as before.

## Use profile

Shown in the page header as "Profile not set up yet" or "Profile used for
tailoring". Once a story exists, the tailor, the cover letter, the form
answers, and the screen are given `base/applicant.md` and every document in
`base/stories/` (one per role or project, written by the Profile
interview). Until then everything runs on the resume and `base/profile.md`
alone. Clicking the label opens the Profile tab.

## Projects from GitHub

The Projects tab takes a GitHub handle and reads every public repository
on it: the README, the file tree, the manifests, the heads of a few
files, and how many commits are yours (forks and empty repositories are
listed as skipped). Each gets a short write-up and one to three
questions the repository cannot answer; the list is ranked, the best ten
are ticked, and any repository that is a project on your resume is
ticked too. Confirm, and the Profile interview asks those questions,
folding a resume match into that project's story ("Resume name
(repo-name)") and making the rest new ones. The link on each project is
the repository unless you change it (a store listing, a live site); it
is what the tailored resume links when it swaps that project in. Public
repositories only; no token needed, though `AUTOPILOT_GITHUB_TOKEN`
raises GitHub's hourly limit past about fifty repositories.

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
