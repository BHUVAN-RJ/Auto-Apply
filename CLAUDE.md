# Auto-Apply — working notes for Claude

Semi-autonomous job application pipeline. Read [PLAN.md](PLAN.md) for the
design and [README.md](README.md) for what it does. This file is the part that
is not obvious from the code.

Repo: `~/Desktop/job-autopilot`, pushed to `github.com/BHUVAN-RJ/Auto-Apply`.

## If this is someone's installed copy

Autopilot is shared by pointing a person's own Claude Code at this
repository (INSTALL.md). If you are that Claude, on a clone whose branch
is `mine`:

- Install and first run: **INSTALL.md**. Changing their code and updating
  it: **UPDATING.md**. Follow them; they are how a change survives the
  next release.
- Their data (resume, profile, stories, applications, edited prompts, the
  OpenRouter key) is in `~/Library/Application Support/Autopilot`
  (`AUTOPILOT_HOME`), never in the clone. Never copy it in, commit it or
  push it; the repository is public.
- Commit their changes on `mine`, one per request, logged in
  `LOCAL_CHANGES.md` with the intent. `scripts/check.sh` after every
  change; a failing invariant means the change is undone.
- A request about *how the models write* (tone, length) is a prompt
  change: point them to the Prompts tab and its Workshop, or make it
  there, rather than editing a stock `*_rules.md` in the clone (that one
  conflicts on every update). A request about *what counts as a reject*
  is a **screening rule**, not a prompt and not code: the Screening tab
  writes one from a sentence, checks it, and previews it against their
  own past postings. Never edit `tailor/screening.py` in their clone.
- The rule below holds on every copy, whoever asks.

The rest of this file is the maintainer's notes on how the code works.

## The rule the whole project exists to protect

**The agent never submits an application, and never closes a job.** Both are
enforced in code, not in prompts, and both have been broken by accident once
already. Before changing anything in `browser/` or the status transitions, read
the Invariants section of PLAN.md.

Specifically:

- **The person may press Submit from the review page** (2026-09-26,
  `browser/press_submit.py` + `POST /review/{id}/submit`, the green
  **Submit it** button on a filled job). This is checkpoint 2 taken from
  the page instead of from the form's own tab, and nothing about the rule
  above moves: no model reaches the endpoint, no browser-use action is
  registered for it, `apply.py` and everything under `browser/` are
  forbidden from importing the presser (`tests/test_invariants.py` #6),
  and the guard still refuses every submit control the agent sees. The
  same `guard.SUBMIT_PATTERNS` that the agent may never click is what the
  presser aims at; `choose` skips a cancel / save-for-later / sign-in
  label, prefers the form's own `type=submit`, and takes the lowest on the
  page, since a multi-step form puts its final action last. The press is a
  real `Input.dispatchMouseEvent` at the control's centre, not
  `el.click()`. **A press is not a submission**: the job is marked
  SUBMITTED only once the page becomes a confirmation (the form gone and
  `watch.CONFIRMED` in its text, the same two halves as everywhere else),
  within 25 s; otherwise the job is left exactly where it was and the page
  says the form is still on the screen. The tab is never closed here.
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
- **Checkpoint 1 passes itself when `auto_fill` is on** (`data/settings.json`,
  default on, 2026-09-18). `pipeline.auto_approve`: clean tailor and a screen
  that is not `reject` → `approved` and `runner.start_fill`, no click. A
  poor fit or a `reject` screen still waits. The human's decision is
  checkpoint 2, the filled form; the fill never submits regardless.
  **Per job:** the banner has an `auto-approve` box next to "Add to
  autopilot", checked by default; unticked during the 3 s countdown the
  job waits at checkpoint 1 (`Job.auto_fill`, sent with `/capture`; on
  Jobright's posting page it goes ahead by `jr_id` through `POST /prefs`,
  in memory, used once, because the employer tab does the queueing).
  `None` on the row = the global switch. Nothing about it presses Submit.
- **The model is per job, and the button does not rush you** (2026-09-24).
  `Job.tailor_model` holds a resolved model name; `None` = the cheap
  default (`OPENROUTER_TAILOR_MODEL`). The banner's green **Use Opus**
  arms it during the countdown *without* cancelling the countdown, so one
  click is the whole decision; the countdown is 3 s rather than 2 for
  exactly that reason. `pipeline.process` hands the name to
  `tailor.tailor(model=)` and `cover.write(model=)` — the resume and the
  letter, nothing else: the screen, the story picks and the form answers
  stay cheap. It travels like auto-approve (`/capture`, else `jr_id`
  through `POST /prefs`, used once), and the review thread's "Re-tailor
  with Opus" (`ThreadMessage.premium`) sets the row first, so the job
  stays an Opus job; pressed with an empty box it sends a fixed "same
  posting, same profile, same rules" instruction, since asking for the
  stronger model is a request on its own. `OPENROUTER_PREMIUM_MODEL` in `.env` names it;
  never hardcode a model at a call site.
- **The flow, end to end (2026-09-18):** Jobright posting page → banner
  screens → countdown presses Jobright's Apply → employer tab opens →
  banner screens it → `/capture` queues it → the employer tab closes
  itself (`closeSelf` in `content.js`, `/__close` on the injector,
  `close-me` on the extension) → pipeline tailors → checkpoint 1 (auto
  when `auto_fill`) → `apply.py` opens a **new** tab on the posting,
  presses Autofill, puts the tailored resume on, stops. The Jobright tab
  stays; only the tab Apply opened goes, and only when the job was
  created by that capture.
- **`find_tab` / `find_target` match the form, not the path**
  (`autofill.same_page`: host + path + query minus `jr_id` / `utm_*` /
  `gh_src` and the like). Greenhouse's embed is one path for every job;
  matching on it put pallet's resume and cover letter on Amira's form,
  three times, before Amira's own fill ran. `run_documents` clears an
  occupied cover-letter slot too, so a stray file never stays behind.
- **`AUTOPILOT_AGENT=0` (set in `.env`, 2026-09-18): no model on the
  form.** Jobright's autofill, the documents by code, a CDP screenshot,
  `form_fill.json`, stop; `filled` iff the resume read back on the slot,
  else `failed` and a red banner. `_finish_without_agent` in `fill.py`.
  `1` brings the browser agent back for the rest, unchanged.
- **The documents go on in code before the agent** (`forms.upload_documents`
  in `fill.py`, `AUTOPILOT_DOCS_BY_CODE=0` turns it off): once Jobright is
  done in the reused tab, the tailored resume over whatever it attached and
  the cover letter where there is a slot, `DOM.setFileInputFiles`, read back.
  **Then the free-form questions** (2026-09-18): `Engine.answer_questions`
  sends every textarea, and every text box whose label has a `?` or is
  over 60 chars, to the tailor model (`answers.Answerer`, same context as
  the resume and letter) and writes the reply into the box (`SET_TEXT_FN`,
  typed as fallback), **over whatever Jobright wrote there**: its autofill
  fills these with generic prose (Perplexity, 9/10 fields, first run), and
  ours goes over it the way the tailored resume goes over its resume.
  Short labels are fields, not questions; visa questions are skipped before
  anything. `Report.answered`; the notes say `answered: …`; the answers
  land in `answers.md`.
- **Every field the automation set carries the assistant's logo** in
  front of its label (`MARK_FN` + `LOGO_SVG`, `Engine.mark`,
  `data-autopilot-mark=<ref>`, `title="Filled by Autopilot: …"`): the
  review page's orb at rest, ink ring, hard offset shadow, purple core,
  drawn static at 18px with fixed colours. The resume slot, the cover
  letter slot, each answered question, each profile field the code filler
  set. Jobright's own autofill gets no logo; those are theirs. Not a plain
  dot: the logo is the point.
  Greenhouse drops the resume input once a file is on the slot;
  `Engine.remove_attached` presses the slot's "Remove file" (label through
  `guard.describes_submit` first) so the input comes back. A miss leaves the
  step to the agent's task as before. Jobright uploads the user's resume
  under the same filename, so a name match alone never proves it is ours;
  ours is the one set after theirs.
- `filled` requires the agent to have actually finished **and** a successful
  `upload_file` of the tailored resume in the action log. The model has
  claimed "resume attached" when its upload was refused; the claim is never
  trusted.
- **One fill per job.** `runner.launch("apply.py")` returns None while one
  is alive (`runner.fill_pid`, tracked Popen plus a `pgrep` fallback).
  `/fill` answers 409 `fill_running` and needs `force`, which the page sends
  only after "This will kill the current job and restart" is confirmed.
  Approve plus "Fill the form" once produced two agents on one tab.
- **The filler never answers a visa question, even from `base/form.json`.**
  The preliminary interview collects authorisation (status, sponsorship,
  dates) because every form asks and the screen wants it, but those keys
  are not in `forms.profile.KEYS` and `describes_protected` skips the
  field before matching. Flipping that is a decision, not a bug fix.
- **A submission is marked four ways, all behind a human's click on
  Submit.** The "I submitted it" button; `POST /review/{id}/submitted-seen`
  from `capture/content.js` when the filled form's tab shows a
  confirmation with no form left (`CONFIRMED` regex plus `FORM_GONE`, job
  id kept in `sessionStorage` across the navigation); `server/watch.py`
  reading the same off the tab over CDP every 4 s (2026-09-20, for when
  the script is not in the tab); and `/screen` recognising a confirmation
  page by its text before any model call (`screen.confirmation_quote`).
  All four go through `review.mark_seen` / `mark_submitted`, mark only a
  `filled` / `filling` job, write no correction, and never close the
  browser. The agent reaches none of them.
- **Adding a job never changes the active tab** (2026-09-20). The banner's
  `openReview(id, { focus: false })` points the Autopilot tab at the job
  (`/__open` with `focus`, `Target.createTarget` `background`, the
  extension's `active: false`); only "Open in autopilot" focuses. The
  fill's own tab, on approve, is what comes to the front.
- **Jobright's panel stalls on Oracle** at ~85% and never reports done;
  the agent waited 15 s a step for seven steps. The task now says two
  identical readings = finished, and `browser/autofill.py` waits on the
  field count, not the panel.
- **Nothing closes the browser any more** (2026-09-19). `/submitted` closes
  only that job's form tab (`chrome.close_tab`, `/json/close/<id>`, the id
  from `form_fill.json` else the tab on the same form). The browser is the
  person's working set: the Jobright list, the next form, the logins;
  quitting it on every submission read as "the whole browser crashes".
  `chrome.close()` (CDP `Browser.close`) still exists, unused by the server.
  The agent still never closes anything.

## Layout

| Path | What |
|---|---|
| `paths.py` | **code and data apart** (2026-09-24). `ROOT` is the clone; `HOME` is `AUTOPILOT_HOME` (the installed app: `~/Library/Application Support/Autopilot`), else the clone. `BASE`, `DATA`, `APPLICATIONS`, `PROMPTS`, `ENV_FILE` hang off `HOME`; nothing else may build a `base/` or `data/` path from `ROOT`. `load_env()` reads `HOME/.env` first, then the clone's |
| `tailor/prompts.py` | **every system prompt, through one door** (2026-09-24). `CATALOG` names 29: the `*_rules.md` files and the reply formats and small prompts that are Python constants. `text(name)` is what every call site sends: the person's edit (`data/prompts/<name>.md`) else the stock text, read at call time so a test's monkeypatched `RULES` still counts. `save` keeps the replaced text in `.history/`, the stock text the edit was made against in `.base/`; `sync()` at server start three-way merges edits onto changed stock text (`git merge-file`); a conflict leaves the edit in use and writes `.conflict/<name>.md`. Adding a prompt: a `Prompt` row here, `prompts.text()` at the call site, never `RULES.read_text()` |
| `tailor/workshop.py` + `workshop_rules.md`, `learn_rules.md` | **the self-improving part** (2026-09-24). `propose(request)`: the cheap model picks ≤3 prompts (`workshop.pick`), the premium model (`OPENROUTER_WORKSHOP_MODEL` overrides) returns SEARCH/REPLACE blocks, applied in code (exactly one match each, one retry with the misses named), a unified diff stored as a `pending` proposal in `data/workshop.json`. `apply` re-applies against the prompts as they are then (`stale` if they moved) through `prompts.save`. `learn()` reads every thread's `change` turns (not the fixed Opus instruction), new ones plus 20 earlier, and proposes standing preferences with the quotes as evidence; `maybe_learn()` runs it in the background after a re-tailor once `LEARN_EVERY` (3) new ones exist, behind `settings.learn_prompts`. Nothing applies without a click. Measured live: a two-rule cover-letter request, 4 s, one clean edit |
| `server/prompts.py` | `/prompts` (list, detail, save, reset, restore), `/workshop` (list, propose, learn, apply, dismiss), `/setup` (key / Jobright extension / resume / chrome / `onboarded`). **Onboarding** (2026-09-25, `Onboard` in the page): until `settings.onboarded` (or any job exists), the page is a wizard instead of the tabs: what it is, the key, the resume (the person's `.tex`, uploaded and checked), Jobright, then the profile interview *offered*, never required ("Start" or "Skip for now", both `POST /setup/done`). The extension is detected from the Autopilot Chrome's profile folder (`Default/Extensions/odcnpip…`); the Jobright sign-in is the person's tick, never read from cookies, and stays that way (decided 2026-09-25: no automatic check, not even by asking Jobright's page). `POST /setup/open {page}` opens one of three named pages as a tab in the Autopilot Chrome over `/json/new`, never an arbitrary URL. After onboarding, the yellow bar lists whatever is still missing and `POST /setup/key` (shape check, `GET openrouter.ai/api/v1/key`, written to `paths.ENV_FILE` 0600 and `os.environ`, so the scripts the server starts see it) |
| `server/report.py` + `.github/ISSUE_TEMPLATE/` | **Report** (2026-09-25, header button): a prefilled GitHub issue, never sent by the app. `GET /report` = diagnostics with nothing personal (version, macOS, setup flags, job counts by status, edited prompt names, non-secret switches, the last five errors as ATS host + first line through `redact`: emails, links to their host, phone-like numbers, home-folder names). `POST /report/open` redacts again (the block is editable), builds `issues/new?template=problem.yml|feature.yml&title&what&diagnostics` under 7.5k characters and runs `open` so it lands in the default browser, where the person is signed in to GitHub. Labels come from the templates, since a `labels=` query needs triage rights |
| `scripts/` | `install.sh` (Apple Silicon + Homebrew + Chrome checks; brew `uv tectonic espeak-ng whisper-cpp`; `uv sync`; the data folder with templates and a `.env` with `AUTOPILOT_AGENT=0`; compiles the master resume; builds the app; quick check), `autopilot` (start/stop/restart/status: server on 8787 or the next free port, recorded in `run/port`; the injector, which launches Chrome and opens the page; logs in `logs/`), `make_app.sh` (`~/Applications/Autopilot.app`, a login-shell stub that runs `autopilot start`; built locally so never quarantined), `check.sh` (invariants, then the suite without `test_fill.py`), `update.sh` (a release tag merged into `mine`, backup tag first, rerere, the invariant files take the release's side, `--continue` / `--abort`) |
| `pipeline.py` | queued job → scrape → tailor → compile → cover letter → archive → checkpoint 1. Started by `/capture`. `process(..., keep_status=True)` (2026-09-23, through `retailor`) is the re-tailor of a job that is already filled or submitted: new folder, new documents, the queue row pointed at them, the status **left where it was**, no `auto_approve`, no fill. The new resume is there to be handed over by hand |
| `apply.py` | approved job → named copies of resume/letter → fill form → screenshot → stop (checkpoint 2). Started by approve |
| `server/thread.py` | **one conversation per job** (2026-09-23), in `data/threads/<job_id>.json`, not in the application folder: a re-tailor writes a *new* folder and the thread is about the job. A turn is a `question` (answered by `tailor/answers.py`, same context as the fill's `answer_question`, the earlier turns added to `Context.thread` — context, never the question, so a visa word said earlier cannot make the next question look protected; still appended to `answers.md`) or a `change` (`pipeline.retailor(..., keep_status=True)` on a worker thread, the turn kept at `running` and polled by the page, one at a time per job). `GET/POST /review/{id}/thread`, and `detail` carries `thread` / `thread_running` |
| `server/` | FastAPI on 8787: queue, review API, `runner.py` launches the scripts. `/capture` starts `pipeline.py` itself |
| `tailor/layout.py` | **the line budget** (2026-09-24): how wide a printed line of this resume is, measured off `base/resume.pdf` (pypdf hands back one rendered line at a time; the 90th percentile of the long lines is the wrap width, cached in `data/line_width.json` by the PDF's mtime, `AUTOPILOT_CHARS_PER_LINE` overrides, 105 without a PDF). `visible()` strips macros and does not count a URL; `budget()` = (printed lines, characters that fit, characters used) and carries `SLACK`, the tolerance the checker judges by. **`room()` is what the model is told** (2026-09-25): the bare rectangle, `lines * width`, never less than what the item already uses, so the spare offered leans low rather than high - the last line of a wrapped item is partly filled and a cap six characters generous is a cap that costs an attempt. The prompt's list is numbered over `_bullets_with_sections`, the same numbering a rejection uses; it was built over `bullets()` before, so "bullet 1" in the budget was "bullet 3" in the rejection that enforced it. The tailor's length rule is this, not characters: a bullet may be rewritten with more words or fewer as long as it prints on the same number of lines. **`PROJECTS` is measured as one block** (`BLOCK_SECTIONS`, 2026-09-24): its entries may move lines between themselves — a swapped-in story may deserve a line the entry it replaced did not — but the section comes back the same height; `EXPERIENCE` bullets, the summary and each skills line are still measured one by one, since one that grows pushes everything under it down. Growing rejects the attempt (cheaper than the compile, and the page is what it protects); losing a line is a warning. **Every budget the checker enforces is listed in the prompt** (`tailor.single_items` + `_build_user_message`, 2026-09-24): the bullets were listed and the summary and the skills lines were not, so a DoorDash run spent all four attempts failing on a summary it had never been given a size for, and a run before it on skills line 7. A model that keeps being rejected stops editing, which is what "the resume is barely changed" looked like |
| `tailor/rules.md` | the tailoring prompt, sent verbatim — edit this, not the Python. **How much changes is measured, and nothing is invented** (2026-09-25, for GLM: 1 of 6 EXPERIENCE bullets rewritten where Opus rewrote 5, on the same prompt). `tailor.under_tailored` counts the EXPERIENCE bullets whose body differs (`rewritten`, matched by similarity like every other bullet comparison) and requires `MIN_REWRITE` of them (half; `AUTOPILOT_MIN_REWRITE` overrides). Under it, the attempt is sent back with the untouched bullets named — but **only while attempts are left**: on the last one it is a warning on the review page, because a thin resume beats a failed job. `tailor._check_invented` is the other half: every **figure** in the tailored bullets, summary and skills lines must appear on the master resume or in the picked stories (never from the posting — a number is a claim about the candidate), and every **name** (a word carrying a capital or a digit) must appear on the master, in a story, or in the posting (adopting the posting's spelling is the point of tailoring). Both sides are read as loose stems, so "Fine-Tuning" is the master's "fine-tuned" and "Dec" is its December; a word capitalised only because it opens a sentence is not a name. Measured over 88 past runs: 4 flagged, among them Qwen's invented "LLM-adjacent". **Method section** (2026-09-21, rewritten 2026-09-24): read the posting into a ranked term list (title/level, required, preferred, responsibilities, repeats; posting's exact spelling), map every term to a resume line or profile story or GAP, then rewrite **every mapped bullet** (not two or three) to the X-Y-Z frame — past-tense verb, result before method, a measure or honest scope, the stack named as the posting names it, one idea per bullet, strongest first under each role, never `Responsible for` — plus the summary, skills order and entry order. **The profile is a door, not decoration** (2026-09-24, after the first run on a filled-in profile changed one project and nothing else): an interviewed story may swap a `PROJECTS` entry (as many as the posting justifies, one out one in, `Link:` required), replace a weak bullet **under the same role/employer**, or trade a technology onto a skills category (one in, one out). Counts never change, nothing is invented, and every line still traces to the master resume or a story. **Every kept `PROJECTS` entry is justified** in the rationale (`Kept/<name>: <posting term>`), so a non-swap is a decision rather than inattention, and a rewritten description is asked to be the shorter one — lines freed that way pay for the entry that needed one. The rationale starts with an `Asks:` line (top five terms); `REPLY_FORMAT` in `tailor.py` asks for it too. **A block section carries its pooled characters** (2026-09-25): the prompt gave `PROJECTS` a total in lines while still listing each entry its own character cap, a per-entry rule to read and a pooled rule to be judged by, so a longer project swapped in looked illegal on the numbers the model had; `tailor.block_budgets` now names what each entry uses, what the section holds and what is spare across it, and `_block_overrun` puts the same arithmetic in the rejection and in `overrun_report` (which skipped block sections, so a second page caused by `PROJECTS` read as "no bullet grew, look elsewhere"). **`tailor.unused_swap` is the swap floor**: a story marked "not on the resume" whose `Stack:` shares `SWAP_TERMS` (2) terms with the posting and carries a `Link:` is expected on the page; returning every entry untouched is rejected with the story and the terms named while attempts remain, and is a warning on the last one. Constraints the checker enforces: sections, preamble, links, counts, and **lines** (`layout.py`) in place of the old ±10-character window. The rationale now also lists what the checker rejected on earlier attempts (`TailorResult.rejections`), so a resume that came back barely changed can be read as a rule the model kept hitting. |
| `tailor/cover_rules.md` | the cover letter prompt, same rule |
| `tailor/byhand.py` | **"Use Opus" without the API bill** (2026-09-27): `build` makes the prompt out of `tailor`'s own pieces so there is no second copy of the rules, `accept` puts a pasted reply through `tailor._validate` and the soft floors. `BY_HAND` is what `Job.tailor_model` holds; it is deliberately not a model name, so nothing can send it to a provider |
| `tailor/cover.py` | letter from the tailored resume; plain pdflatex template; failure is non-fatal |
| `outreach/` + `server/outreach.py` + `tailor/outreach.py` | **recruiter outreach** (2026-10-04): Apollo finds a recruiter first and a hiring manager second, Jobright's Find Any Email is the fallback guess, the review page writes a carousel of drafts after the cover letter, and Gmail sends only after **Approve and send**. Lookup and drafting are automation; delivery is not. Guessed or typed addresses need "I verified this address". State is `data/outreach.json`, sent records are immutable beside the resume, and nothing in `pipeline.py`, `apply.py`, `browser/` or `tailor/` may import `mailing`. |
| `mailing.py` | shared SMTP for Scout digests and approved outreach; attachments and an external recipient live here. `scout/mail.py` keeps the digest wording and still requires `AUTOPILOT_MAIL_TO`. |
| `tailor/answers.py` + `answer_rules.md` | free-form form questions, answered by the tailor model (`OPENROUTER_ANSWER_MODEL` overrides) via the agent's `answer_question` action, and **by the human from the review page** (2026-09-18): `POST /review/{id}/ask` builds the same `Context` the fill would (posting, tailored resume, letter, profile with the picked stories, facts), the "Ask for an answer" card shows the reply with Copy, and `detail.questions` (`answers.open_questions` over `form_state.json` / `form_fill.json`: empty, `?` or > 60 chars, never visa) are one-tap prompts. Everything lands in `answers.md`; the page shows the whole file. Nothing is typed into the form by this path |
| `tailor/screening.py` | **the rules the screen runs, in code** (2026-09-26). A `Rule` is a category, a severity, and either `patterns` (regexes, any one fires it) or a `check` in `CHECKS` for the four that need more than a phrase (`experience_years` reads the floor of every "N years" and compares it to the rule's `value`; `seniority_title` reads the title only, and never when it also says Member of Technical Staff / Associate / new grad; `location_foreign` reuses `scout.filter`'s country lists; `graduation_window` hands the sentence to `screen.py`'s date reading). `unless` cancels a match in the same sentence, which is how "we cannot guarantee sponsorship" stops being a refusal; a match whose sentence is a question is passed over, not accepted, because "Will you require sponsorship?" is on half the forms in the country. `STOCK` is the catalog, `data/screening.json` holds the person's switches, values and own rules, read at call time. `invalid()` refuses a pattern that does not compile or that matches ordinary prose. `AUTHOR_PROMPT` is the tab's assistant - it writes rules, never verdicts |
| `tailor/screen.py` | on-page auto-reject screen: `screening.fired` over the posting, then the two policies that correct a flag (`enforce_location_policy`, `enforce_timeline_policy`), then `verdict_for`. **No model** (2026-09-26). Facts from `base/applicant.md`, softened to `soft` when they were derived from the resume (`soften_unknowns`), because a resume knows nothing about visas or dates |
| `server/screening.py` | the Screening tab's API: `/screening` (list), `/screening/rule/{name}` (switch, value), `/screening/rule` (add / delete one of the person's own), `/screening/preview` (what a rule would have done to the postings already in `applications/`), `/screening/ask` (one model call, writes a rule from a sentence). Nothing is saved without a click, and a proposal is previewed against the corpus first: a rule that would have flagged a third of past jobs says so |
| `server/screen.py` | `POST /screen`, URL-keyed cache in `data/screens.json`; the pipeline reuses it. **A confirmation page never reaches the model** (2026-09-20): `confirmation_quote` (the `watch.CONFIRMED` phrase, in a short page or near the top of a long one) answers `verdict: submitted` in string work, and when `seen` says the page is a filled job of ours, `review.mark_seen` marks it right there. Employer URLs reuse a cached Jobright verdict; without one, low-quality ATS text is combined with the saved Jobright copy in one model call |
| `server/keys.py` | **the Settings tab: every key in one place** (2026-10-08). A key used to be typed wherever it happened to be wanted - OpenRouter in the onboarding wizard, Apollo and the Gmail App Password on one job's Outreach card - so there was nowhere to see what the app holds, nowhere to replace a rotated one, and no way to add the Apollo key before a job existed to add it from. `CATALOG` is the seven: the OpenRouter key (the only required one), Apollo, the Gmail address / App Password / digest recipient, a GitHub token and the Fish Audio key, each with what stops working without it and where to get one. **A key is written, never read back**: `GET /keys` says `set` and the last four characters, and the value is not in the response anywhere (`test_a_saved_key_is_never_handed_back`). `POST /keys/{name}` checks the shape - a line break in a paste is a typing mistake - and writes through `prompts.write_env` to `paths.ENV_FILE` 0600 **and** `os.environ`, so the server and every script it starts see it with no restart; an App Password loses the spaces Gmail shows it with. `POST /keys/{name}/check` is separate and is the person's click, because a check costs a request to somebody else's service and, for SMTP, a login their provider may count; it covers the whole account, so checking the App Password logs in with the address too, and sends nothing. A refused key is 400 and a dead service is 502, because reading them the same way sends somebody to re-copy a key that was fine. The required key can be replaced but not removed. **Switches are not here**: a key is a credential the person holds, `AUTOPILOT_SPLIT=0` is a decision about behaviour |
| `server/settings.py` | `use_profile` in `data/settings.json`; the page pins it on, the header only reports whether a story exists |
| `tailor/profile.py` | what the models are told about the applicant. `context(slugs)` = profile.md + applicant facts + `base/stories/index.md` + the picked stories' `tailor.md` when the switch is on, profile.md alone otherwise. `pick(posting)` chooses the slugs (one cheap call); the pipeline records them in `stories_used.txt` and the cover letter and answers reuse them. **The slots are split** (2026-09-25): `SWAP_SLOTS` (3) stories the master resume does not carry and `DEPTH_SLOTS` (2) that it does, candidates first, the pool backfilled by `stack_overlap` from the stories the picker passed over so a run is never out of stock. `on_resume(slug, resume_tex, story)` decides which is which - the story's `Link:` on the page, else 60% of the slug's own distinctive words in the resume's visible text (exact on the 15 stories on file). `stories()` says it per story in the prompt, because a story already on the resume can only deepen its entry while one that is not may take a `PROJECTS` entry's place. `screen_facts()` falls back to facts derived from the resume, cached in `data/derived_facts.md` |
| `tailor/interview.py` + `interview_rules.md` | the profile interviewer. **`with_stack_line`** (2026-09-24): a story's `Stack:` line is taken from its GitHub scaffold when the conversation never named one — an interview about *what you built* rarely lists the stack, and the tailor matches a posting's languages against exactly that line; two swap candidates read `Stack: not discussed` while their scaffolds, read off the repo's manifests, had the whole list. The scaffold fills an empty line and never overwrites what the candidate said. The rest: state machine on disk under `base/stories/` (`_interview.json`, `<slug>/state.json`), one streamed turn per candidate message, header (`COVERED` / `DONE`, then `---`) parsed in code; the body is labelled by line (`ack:` and `ask:` spoken, `note:` text only; `_Parts` strips labels mid-stream and tags each delta `spoken`), and the page speaks only tagged parts, falling back to first sentence plus questions when a reply has no labels. **The openings written in code are labelled too** (2026-09-21, `_opening_parts`): the greeting is an `ack`, the resume's role and project lists and the target-roles line a `note`, the question after them an `ask`, so the voice never reads the bullets out. Writes `main.md` on close; `story_rules.md` is the prompt for `tailor.md` and `star.md`, written by the heavy model in a thread |
| `tailor/facts.py` | the facts interview, first thing after Start: eight fixed questions in code (authorisation, clearance, level, location, relocation, start date, graduation, form details). **Answered from `base/form.json` first** (2026-09-21, `from_form`, no model): the preliminary interview already holds authorisation, clearance, location, start date, graduation and the contact links, so those six are written as literal lines and only what the form lacks (level, relocation) is asked; `settled()` is the count the pill shows. Resume-derived facts and contacts offered as hints, each answer normalised to one literal line by the cheap model (`NORMALISE_PROMPT`, one follow-up max, "skip" = unknown), then `base/applicant.md` written. State in `base/stories/_facts.json`. `interview.skip_facts` / `restart_facts` (`/profile/facts/skip`, `/restart`); a redo mid-stories returns to `State.resume_phase`. The header pill shows Facts and Stories separately |
| `tailor/github.py` + `github_rules.md` | projects from GitHub (2026-09-20): `start_scan(handle)` lists the handle's public repos (forks and empty repos become `issues`), counts the handle's commits (one API call each; none attributed = counted whole and noted, the email is not linked), pulls each tarball off codeload (no git, no token, 40 MB cap), and the cheap model writes `base/stories/_github/<repo>/scaffold.md` plus 1-3 questions the repo cannot answer. `score`/`rank` in code (recency, stars, commits, README); the top `AUTO_PICK` and every repo matching a resume project are ticked. `confirm` → `interview.add_github_projects`: a match on a resume project folds the scaffold into that story ("Resume name (repo-name)", `start_fold` when it is already closed), the rest become experiences whose checklist is the questions (`Experience.questions`, lines `g1..gN`, closed in code after the last answer, no wrap-up); the interview reopens from `open` for them. A project carries **several links** (2026-09-22): `Repo.links` (the repository is always first) plus `Repo.link`, the one ticked; `add_link` / `remove_link` / `set_link` on `POST /{name}/link {link, action: choose|add|remove}`, pushed to the experience by `interview.set_links`. The chosen one alone is the `Link:` line in `main.md` and `tailor.md` (rewritten in code) and the only URL `tailor._check_links` allows; the repository's own URL cannot be removed and is the fallback when the chosen one is. State in `base/stories/_github.json`. `AUTOPILOT_GITHUB_TOKEN` raises the rate limit; `OPENROUTER_GITHUB_MODEL` picks the reader |
| `server/github.py` | `/projects` status, `/scan`, `/{name}/scaffold`, `/{name}/pick`, `/{name}/link`, `/confirm`. The Projects tab (third header tab) polls it |
| `server/profile.py` | `/profile` status, `/profile/start`, `/profile/turn` (SSE, one JSON event per line), documents, regenerate |
| `voice/` + `server/voice.py` | speech: whisper.cpp in (`stt.clean` drops whisper's `[BLANK_AUDIO]`-style markers, standalone um/uh/erm/hmm and immediate word repeats); Fish Audio out when `AUTOPILOT_FISH_API_KEY` is set (`fish.py`, hosted, one request per sentence, `s2.1-pro-free` by default, voice pinned by `reference_id` because the API otherwise picks a new voice per request; delivery tuned by ear: `(cheerful)` tag, temperature 0.9, speed 1.08, all overridable in `.env`); Kokoro (ONNX, `kokoro.py`, needs brew `espeak-ng`) out, `say` when Kokoro is not ready, Piper via `AUTOPILOT_PIPER_BIN`. `assets.py` finds binaries and downloads models into the app data dir; `/voice/status`, `/setup`, `/transcribe`, `/speak` |
| `capture/content.js` | reads the page, calls `/screen`, paints the banner. Runs on Jobright posting pages (`/jobs/info/`) and on any tab opened from Jobright, either as the extension's content script or evaluated by `browser/inject.py` (then it talks to the server through the `__autopilotRequest` binding, not `fetch`). An `ok` or `caution` verdict counts down 2 s and acts by itself: off Jobright it queues the job, on Jobright's posting page it presses Jobright's Apply so the employer tab queues itself; a `reject` waits for the click. Successful Add carries the job id into `openReview(id, { focus: false })`: the Autopilot tab is pointed at the job (or created in the background) and **the active tab does not change** (2026-09-20); only "Open in autopilot" focuses it. The fill's own tab, on approve, is what comes to the front. After 5 s the bar collapses into a persistent, verdict-coloured assistant badge; expanding it reuses the same DOM/result and never screens again. Failed and `not_a_job` banners still offer Again and Add |
| `browser/inject.py` | the capture without an extension: attaches to our Chrome on 9333, evaluates `capture/content.js` in Jobright tabs and the tabs its Apply opens (by opener, or by the `?jr_id=` tag Jobright's extension puts on the URL), and answers the page's `__autopilotRequest` binding by making the HTTP call itself; the `/__open` path focuses/navigates an existing review-page tab or creates one over CDP (sites swallow `window.open`). `AUTOPILOT_SERVER_URL` changes the local origin and the injected script together when 8787 is occupied. Reconnects with backoff while 9333 answers, exits when Chrome is gone. Run by hand: `python -m browser.inject --open <url>` (lives in a Herdr pane). Never launches through browser-use, never closes the browser. **The one tab it closes** is the employer tab Apply opened, at that page's own `/__close` request once `/capture` has *created* the job (`Injector.closable`: a marked tab, never Jobright, never the review page; an "already in autopilot" page never asks, since a fill may be on it). The extension does the same with `chrome.tabs.remove` on `close-me`. **It also keeps the two halves of the split** (2026-10-07): the source list is placed on the left half of the screen and the job its Apply opens is moved into one reused window on the right (`split_out`, `place`, `focus_source`), because no ATS and not even our own page may be framed. `is_new_job` is the gate - a tagged URL whose posting already has a queue row is the *fill's* tab and is never moved. `AUTOPILOT_SPLIT=0` off |
| `server/seen.py` | is this posting already in autopilot: `high` (same canonical URL, same `jr_id`, same ATS job id) or `confident` (same company and either title similarity ≥ 0.8 or posting simhash within 6 bits; rejections stop matching here after 90 days). `/screen` carries it as `seen`, the banner then never counts down and offers "Open in autopilot" (plus "Add anyway" at `confident`); `queue.add` dedupes by `same_job`; `/capture` refuses `jobright.ai` URLs outright. **A job already applied for is shouted, not mentioned** (2026-09-25, `appliedNotice` in `content.js`): `submitted`, `filled` or `filling` (`APPLIED_STATUS`) paints a red bordered block, "ALREADY APPLIED", with the date, the company and whether the match is certain, and **that bar never collapses into the badge** — one line inside a bar that folds itself away after five seconds is how the same job got applied for twice. A certain match still has no Add button; `confident` keeps "Add anyway", since it may be another role at the same company |
| `tailor/quality.py` | posting text without a model: strips boilerplate lines, scores words + section headings + bullets; `pipeline.fetch_posting` picks the best of the fetched page, the browser's text, and Jobright's copy (fetched wins at ≥ 60% of the best) and records `**Text from:**` in `posting.md`; `simhash` for the seen check |
| `server/postings.py` | posting text the browser saw, for when the fetch gets a shell: `POST /posting` keeps Jobright's copy by posting id, `/capture` keeps the employer page's text; `pipeline.fetch_posting` falls back in that order. Metadata comes from `<role> @ <company> | Jobright.ai`, or the visible company / age / role header after "Original Job Post" when Jobright leaves `document.title` generic |
| `browser/autofill.py` | step one of the fill in code, no model: opens the form in a tab on our Chrome over raw CDP, finds the one control whose text starts with "Autofill" (page and every shadow root; Jobright's panel is a custom element), checks it against the submit deny-list, clicks it once, waits for Jobright's own word (`LISTEN_JS`: their extension posts `updateResultFromIframe` / `autoFillResultFromIframe` / `autoFillCompleteFromIframe` to `window.top` with `filledFields` / `missingFields` / `currentField`; read out of their bundle in `~/Library/Application Support/job-autopilot/chrome/Default/Extensions/odcnpip…/helper-app.*.js`), else its panel, read out of the `plasmo-csui#jobright-helper-plugin` shadow root (page `innerText` never sees it): `Autofilling` with three dots while it runs, then `N/M required fields filled` once it stops, so `panel.done` after `panel.busy` (or a count sitting untouched `SETTLE_POLLS`) is the deterministic finish; the filled-field count holding still is the last resort and never while the panel is busy. `from_status` reuses a tab on the panel count too, and never while it is busy. `missing` goes into the agent's task as "Jobright itself reported these empty". **Pressed by the fill, in its own fresh tab, on approve** (2026-09-18): the employer tab Apply opened is screened, queued and closed, so there is nothing to reuse. `inject.maybe_autofill` (press on load, `AUTOPILOT_AUTOFILL_ON_OPEN=1` brings it back) is off by default; when it is on, the fill `reuse`s that tab (`find_tab` by URL, Jobright's messages read back off `window.__autopilotJR`) instead of opening a second one, and only presses itself when no such tab exists. Jobright's panel has two steps on some pages ("Autofill my application" opens it, "Autofill" inside starts the fill), so a differently worded control gets one more press while nothing has happened (`MAX_PRESSES`). `Page.setInterceptFileChooserDialog` is on for the duration of the press: a native chooser froze a tab and every script in it. `signal_js` / `notify` tell the page's banner what automation is doing (`window.__autopilotAutomation(state, note)`): `working` = purple pulsing core + note line, `done`, `error`. The agent then `switch`es to that tab and its task says step one is done. Any failure = `clicked=False` and the agent does it as before. `AUTOPILOT_AUTOFILL_BY_CODE=0` turns it off |
| `browser/forms/` | the fill without a model, for Ashby, Greenhouse and Lever (`adapter_for(url)`, one module per system, `ADAPTERS` in `__init__`). `engine.py` scans every control over raw CDP (label resolved like a screen reader, `data-autopilot-ref` tags), matches by the adapter's id/name selectors, then `base/form.json` `answers` by exact label, then generic label patterns (never on a textarea or a label over 60 chars: those are questions), sets text through the native setter with real typing as fallback, `<select>` by option text, radios and Ashby's `button[aria-pressed]` by click, comboboxes by click + keys + clicking the suggestion, files by `DOM.setFileInputFiles` read back off the input or its block. Visa fields are skipped before matching whatever the profile says; no option or radio is clicked without `guard.describes_submit`. The report (`form_fill.json` next to the screenshot) is the agent's task: "these are still empty". `base/form.json` is gitignored, template `base/form.example.json`; missing = Jobright path as before. `AUTOPILOT_FORM_FILL=0` turns it off, and **it is off in `.env`** (2026-09-18): Jobright's autofill is always step one, this stays as the fallback |
| `server/corrections.py` | the correction loop, **human-picked** (2026-09-20, replaces the silent diff-on-submit: "a very bad way for the AI to learn"). `browser/fill.py` writes `form_fill.json` with `after_agent` (the form as the fill left it, `forms.snapshot` over CDP, visa questions dropped) and `after_agent_meta` (the control's id / name / kind per label); the filled tab's `content.js` pings `POST /review/{id}/form-state` every 5 s, on submit clicks and on `pagehide`, and the server looks at the form itself (`capture`; `server/watch.py` does the same without the page) into `form_state.json`. `changes(app_dir)` = the diff, rows `{label, was, now, remembered}`, returned on every `/form-state` reply, on `GET /review/{id}/changes` and in `detail`. The banner (`CHANGES` / `paintChanges` in `content.js`, pops the bar open once when the first row appears) and the review page's "What you changed on the form" card show the rows with a tick each; **Remember** → `POST /review/{id}/remember {labels}` → `remember()` → `forms.profile.add_corrections` for the picked labels only, records (`value`, `was`, `system`, `field`, `when`, `job`) in `base/form.json` `corrections` (old `answers` key folded in on read, dropped on write). `/submitted` and `mark_seen` take one last look and return the rows; **they write nothing**. **Applied on the next fill:** `Engine.apply_corrections` in `run_documents`, after the documents and before the questions, puts each corrected value over whatever Jobright left on any field whose label matches, never a textarea, never a visa question, logo note "corrected: … (autofill had …)"; `Report.corrected`, the notes say `corrected: …`; `AUTOPILOT_CORRECTIONS=0` turns it off. The Profile tab's Form details shows the whole table (field, form filled, you corrected, where) with delete. **Behind the auto-learn switch, off by default** (2026-09-21, `settings.auto_learn`, the checkbox at the top of Corrected fields): Jobright fixed the autofill this was correcting for. Off, `corrections.changes` returns no rows (no card, no banner nag, nothing to Remember) and the fill loads no store; the table stays readable and deletable. On brings all of it back |
| `server/watch.py` | the server's own eyes on every filled form (2026-09-20), a thread started at app startup (`AUTOPILOT_WATCH=0` off; `TestClient(app)` without `with` never starts it). Every 4 s, for each `filled` / `filling` job with a `form_fill.json`: attach to its tab (`find_target` by the recorded id), `forms.snapshot(detail=True)` (fields, identifiers, visible text, URL). Form still there and on the job's host → `form_state.json`, the file the page's ping writes, so the correction loop has its final state without a single ping. No form and a `CONFIRMED` phrase → `review.mark_seen` (shared with `/submitted-seen`). The phrase alone is never enough: postings say "thank you for your interest"; the form has to be gone (`corrections.MIN_FIELDS`). The page's script does the same from inside the tab; this is for the times it is not there (tab never marked, navigation missed, `sessionStorage` blocked), which was "sometimes it works". Nothing pressed, nothing closed |
| `server/form.py` | the preliminary interview: `GET/POST /profile/form`, fixed `QUESTIONS` (contact, location, work, education, source pinned to Other, EEO, work authorisation), `/profile/form/hints` = contacts from the resume cached in `data/contacts.json`. The Profile tab's `Form` module walks them one per screen (Enter next, Skip, Save and stop), then shows the file and the "Corrected fields" table with delete. Authorisation keys are on file but not in `forms.profile.KEYS`: collected, never auto-filled |
| `browser/ats.py` + `ats_rules.md` | per-system notes for the browser model, picked by URL (oracle, greenhouse, ashby, workday, lever), sent verbatim under "Notes for this application system". Edit the markdown, not the Python. Oracle: one "Upload Attachment" control for every document, so the upload guard lets the cover letter onto a generic attachment slot (never onto a resume-named one) |
| `browser/guard.py` | the never-submit deny-list |
| `browser/open_apply.py` | **the only place Apply is pressed** (2026-09-30). Apply is not Submit, and the difference is checked on the page: `safe_to_press` wants no file input, no password box and fewer than `signin.FORM_FIELDS` editable fields, so there is nothing on it that could be sent; `APPLY_START` must match the control's whole label (including LinkedIn's "Easy Apply") and `NOT_APPLY` disqualifies submit / send / finish / withdraw / save / sign in. `MAX_PRESSES` 3: posting → chooser → "Apply Manually". Does not import `guard`, may not name the submit presser (invariant #6 reads it as text), one gated `Input.dispatchMouseEvent` |
| `browser/workday.py` | **Workday, page by page** (2026-09-30). `walk`: per page, Jobright's autofill, the tailored documents on the page that has a slot, corrections, open questions, then that page's own "Save and Continue", up to `MAX_PAGES`. Ends at the review page, never Submit; `PAUSES` names whose turn it is (account, email verification, a question `blocking()` found, a page that would not move). `stage_of` reads a posting *before* a review page, because `guard.describes_submit` answers yes to "Apply". Writes `workday.json` |
| `browser/linkedin_apply.py` | **LinkedIn Easy Apply** (2026-09-30). Nothing selected by class - LinkedIn's rotate - so the anchors are `aria-label`, visible text and the `N/M pages` the flow prints; the flow is not a `[role=dialog]`, it replaces the page. The tailored resume goes on every time (`put_resume`, slot cleared, name read back); the screening questions are the person's, so it stops at the first unanswered control and lists them; ends at the review page. `linkedin.json`. **Not yet verified live** |
| `tailor/boards.py` | **the four boards read from their own APIs** (2026-10-06). Jobright's Apply lands on the application, not the description: 109 of the 134 Ashby / Greenhouse / Lever / Workday URLs on disk are a form URL (`/embed/job_app`, `/apply`, `/application`). `identify(url)` names the board and the job from either shape; `posting(url)` reads Greenhouse `boards-api`, Lever `api.lever.co/v0/postings` (opening + each named list + additional, in order), the Ashby job-board feed and Workday `wday/cxs`, none of which needs a login or a key. `fetch.fetch` asks here first, before LinkedIn and before any page read. What it fixes, measured: Greenhouse's embed carried the whole form round the description (Axon 3,381 words of which 1,617 are the posting) or was the form alone (ASM, Pinterest, Tenable - 0 section headings, tailored anyway); a Lever `/apply` fetch read the whole board (Palantir, 10,832 words of other people's jobs); Ashby's `/application` truncates the description on some boards (Clay, Pinecone) and appends the self-identification form on all; and Workday, which renders client-side, fell back to a JSON-LD `description` Workday has already stripped of its own markup - 6,367 characters on **one line**, every bullet run into the sentence before it, now 27. A closed job 404s and the ordinary fetch runs as before. `Posting.authoritative` marks the board's own copy and `pipeline.fetch_posting` returns it without a contest, because the browser's copy of a form is longer than the description inside it |
| `tailor/linkedin.py` | the posting behind a LinkedIn URL, from `jobs-guest/jobs/api/jobPosting/<id>` - no login, no key. A fetch of the job page is 1778 words of LinkedIn around 194 of posting. `fetch.fetch` asks here first for `/jobs/view/<id>` or `?currentJobId=` |
| `browser/press_submit.py` | the only place a Submit control is pressed, and the agent cannot reach it: the review page's **Submit it** button, through `POST /review/{id}/submit`. `choose` picks the control, `confirmed` decides whether the page that came back is a receipt |
| `browser/chrome.py` | launches and reuses the Chrome that browser-use attaches to |
| `tex/compile.py` | engine picked per document, not fixed |
| `archive/store.py` | immutable per-application folders |
| `review/index.html` | the whole UI, one file, no build step. **Opens on the first job in flight** (`loadList`: `inflight[0]`, list order needs-you first, then the agent's), never one from a closed shelf; nothing in flight = "Nothing in flight." in the pane. `/#<id>` (the banner's "Open in autopilot") is read at boot and on `hashchange`; it was ignored until 2026-09-19. Terminal look (mono, square, purple = agent, green = you, red = rejected). **The list and the job scroll apart** (2026-09-22): `body` is a flex column the height of the window and never scrolls itself (no magic header height any more), `main > nav` and `main > section.detail` each `overflow-y: auto`; scrollbars are styled square and thin in `--line` (`::-webkit-scrollbar*`, `scrollbar-width/color`), which is also what keeps macOS from hiding them until something moves. Under 800px the page goes back to one column and one scroll; `BUCKET` / `VERB` / `ORDER` at the top drive the in-flight list and the two closed shelves; the floating `.fab` is the decision; nothing internal (models, pids, folders, commands) is shown. Holds the profile state pill in the header, the Profile tab (chat, seed files, documents) and the floating voice orb, always on. Each job has **one thread** (2026-09-23, `wireThread` / `paintTurns`, replaces the one-shot "Ask for an answer" card): Answer it, or Re-tailor with this (confirmed first, then polled while it runs, and the job is re-read when it lands, because the folder changed under the page). Above the documents, **Show the files in Finder** (`POST /review/{id}/reveal`): the named upload copies are made if the fill has not made them yet (`apply.upload_copy`) and both are selected in Finder by one `osascript` reveal, for the forms whose screener never reaches the assistant and whose files are quicker dragged in by hand. **The chat fills the pane** (2026-09-21): `section.detail.chatpane` (set in `renderChat`, dropped by `leave()`, `render()` and the tab switch) is a flex column the height of the window, the transcript scrolls in the middle and the composer sits on the bottom edge; bubbles the full width (`.chat .msg { max-width: none }`, the old 72ch cap read as "half the screen"); the head one line with mic and voice as two dots, the `voicebar` line only when something needs downloading or installing. Not a floater (built and reverted the same day); only the orb floats. The Projects tab re-reads `/profile` while `started` is false, so "Add N to the interview" lights once Start is pressed without leaving the tab. The orb: opening the chat speaks the open question and then listens, a tap pauses (red, "Paused", text only both ways), the orb drags anywhere and remembers its spot, leaving the chat stops everything (`Profile`, `Bubble` and `Voice` modules at the bottom; the orb is a flat SVG ring in the page's ink, page-colour fill, hard offset shadow: a fixed circle plus three standing-wave modes on springs, kicked by the audio level, so it bounces but never changes shape; one flat core inside grows with the level, green only while listening, purple while speaking; thinking is an arc sweeping the rim with a bulge under it (peak at the head, fading to the tail, `ARC`/`BULGE`/`SOFT` in `Bubble`); paused is a red dashed ring with the word, error a red ring; only `ack:`/`ask:` parts of a reply are spoken, with a pause between; silence cut-off constants `SPEECH`, `SILENCE_MS` (1500 ms since 2026-09-22); barge-in is behind `BARGE_IN = false`) |
| `capture/background.js` | context menu, follows tabs off Jobright to inject the screen wherever Apply lands, and focuses/reuses the unpacked extension's existing Autopilot tab after capture |
| `tools/sweep_failed.py` | moves `failed` application folders under `applications/failed/` and repoints queue rows; nothing deleted |
| `tests/dom/` + `tests/test_dom.py` | the only tests that use a browser: a headless Chrome of their own, running the real `SCAN_JS`, `FIND_REMOVE_FN`, `TAG_NAMED_FILE_FN`, `MARK_FN`, `putFile` and the banner's own layout rules against the shapes real forms have (`ashby_application.html` is the live Deepgram form's markup; `workday_two_resumes.html` is the bin icon with no label in it). `scripts/check.sh` runs them under a heading of their own and **says the skip out loud** when there is no Chromium |
| `server/simplify.py` + `capture/tracker.js` | **the Simplify new-grad list, tracked on GitHub's own page** (2026-10-01). `github.com/SimplifyJobs/New-Grad-Positions` is a README of HTML tables, and every row carries the posting's UUID in its `simplify.jobs/p/<uuid>` link beside Apply, so a row is matched to our queue exactly rather than by guesswork; nothing is fetched from GitHub by the server, the page is already open and sends its own rows. `tracker.js` is injected in place of `content.js` on that page alone (`inject.is_list_page`, `LIST_PAGE` in `background.js`, and the manifest): a column of its own saying what happened with each posting (APPLIED, FILLED, IN AUTOPILOT, REJECTED, FAILED, the badge being the way back to the job), an **Add** button on every other row, and a bar that works **one day at a time**. `POST /simplify/status` is read-only and writes nothing; `/simplify/row` adds one by hand (`force`: the person's decision stands in for the screen, the pipeline screens it anyway); `/simplify/day` runs a day in a thread, one day at a time, polled by `/simplify/day/status`. Per row, in order: a banned URL (the list's own pages, never queueable), the legend's own marks (`🛂` no sponsorship, `🇺🇸` citizenship - a certain no must not cost a page fetch), `filter.at_level`, `filter.outside_us`, then `prescreen` and the screen; `ok`/`caution` is queued with the same three steps as `/capture`, so `hold_fills` lands the whole day at APPROVED and nothing opens a tab. **`MAX_PIPELINES` (3) is the other half of that**: the hold holds the *fills*, not the tailoring, so a day of 90 rows started a `pipeline.py` each - 90 fetches, model calls and LaTeX compiles at once on one laptop; the run waits in its own loop (`_room`, `os.kill(pid, 0)`) rather than leaving rows QUEUED for nobody. **"Applied" is derived, never written**: it is that posting's job status in the queue, so the four submission paths stay the only things that decide an application was sent. Only queued rows get a badge (asked for); a row passed over keeps its Add button with the reason in its tooltip. A day's run covers the Software and AI/ML sections (`WORKED`); the other three tables get badges and Add buttons but are not tailored in bulk. The Age column is a day for the first month and then `1mo`/`2mo`, which is 267 of 486 rows: a coarse age is its own bucket, labelled "About 1 month old", and **only a real day has a run button** - one button over `1mo` was 169 postings |
| `scout/` + `server/scout.py` | the Scout tab (2026-09-20): companies' careers pages watched on a schedule, new entry-level roles screened, listed and mailed. **Two kinds of watch** (2026-09-21, `Watch.notify`): a notify watch is a company where a referral is possible, its hits are listed under the tab's Notifications view and mailed with the link and the note, never queued by the scout; every other watch feeds autopilot: a hit whose verdict is in `run.QUEUE_VERDICTS` (`ok`, `caution`) is queued at once by `run.check` through `queue_hit`, a `reject` or a failed screen waits on the Autopilot view for the person. The add form is company + URL + the notify box (referrer shown when ticked). Before the model, `filter.prescreen` rejects in code what is cheap and certain: `YEARS_MIN` (4) or more years of experience (first number of a range), a security clearance, citizenship required; `DEFAULT_POSITIVE` covers the roles next door too (data, backend, full stack, platform, ML, infra). The digest says "N to ask about and M in autopilot", the referral ones first. **A company's own careers page works too** (2026-09-21): when `detect` has no provider for the host, `POST /watch` runs `providers.discover(url)` (the page read once, `BOARD_LINKS` regexes for Greenhouse / Lever / Ashby / BambooHR / Workday / SmartRecruiters links) and makes one watch per board, named "<company> (<provider>)" when there are several (Mujin: a Lever board for Japan, BambooHR for the US). BambooHR is `<sub>.bamboohr.com/careers/list`, description from `/careers/<id>/detail` for unseen ids only. **A watch page lists every role at the level right now** (`Watch.listed`, `run.listed_row`, `LISTED_KEEP` 100, rewritten on each check) with "Add to autopilot" per row (`POST /watch/{id}/add {posting_id}` → `run.add_listed`: a hit, marked seen, `queue_hit`); the tab opens on Notifications and lists them above Autopilot. **`Watch.us_only`** (default on): `filter.outside_us` drops a role whose every listed place names another country (`NON_US`, word-bounded; empty, Remote, an unknown place or any US part keeps it) before the title filter's result counts, so Mujin's Lever board (Tokyo, Netherlands) lists 0 at level. `DEFAULT_NEGATIVE` also drops electrical / mechanical / controls / technician / sales and the like. `/` is served `Cache-Control: no-cache`: Chrome kept an old `index.html` for a session. The page polls every 5 s only while visible; `visibilitychange` polls at once, so a submission marked while the person was on the form shows the moment the Autopilot tab is back (it looked like "submitted is not sent back"; the four paths all fired, the list was stale until the next tick). `detect(url)` picks the provider from the host (`providers.py`, one function each over the public JSON: Greenhouse, Lever, Ashby, BambooHR, Workday CXS, SmartRecruiters, Oracle ORC, Eightfold pcsx incl. Microsoft; and the server-rendered pages of Google, Amazon, Apple) or raises `DetectError`, so a watch that can never list a role is not created (Meta needs a session token; refused by name). `filter.at_level` is word-bounded substring lists on the watch (`DEFAULT_POSITIVE` / `DEFAULT_NEGATIVE`; `III` is deliberately not negative, it is Google's new-grad level). `run.check` fetches, filters, drops `seen_ids`, screens each survivor through `server.screen.screen_url` (cached by URL), records `Hit`s, seeds seen on the first check without a hit or mail; ids no longer listed are dropped so a role that returns is news again. `run.verify` is the same read without writes; a raise or an empty list sets `Watch.error`, the red BROKEN on the tab. Thread `run.start` ticks every 60 s and checks what `due` (24 h / `checks_per_day`, per watch else `settings.scout_checks_per_day`); `AUTOPILOT_SCOUT=0` off. `mail.py` is stdlib `smtplib` with a personal account (`AUTOPILOT_SMTP_*`, `AUTOPILOT_MAIL_TO`); one digest per round for hits not `reject`, `referral_note` is a template, not a model. `run.queue_hit` is the same three steps as `/capture`. State in `data/scout.json` (`store.py`, atomic). `tools/scout_verify.py` prints OK / BROKEN per page and exits with the broken count. **A page that breaks is said three ways**: red `BROKEN` on the Scout tab button from every view (`loadScoutBroken`, `/scout` `broken`), the banner "This company does not work" on the tab, and one mail (`mail.broken_notice`, `Watch.broken_mailed`, reset when it reads again; never one per round) |

Config lives in `.env` (gitignored). `AUTOPILOT_RESUME_FILENAME` and
`AUTOPILOT_COVER_LETTER_FILENAME` there name the uploaded PDFs (spaces become
underscores); the archive keeps `resume.pdf` / `cover_letter.pdf` and a copy
under each name. `base/resume.tex` is the master resume and
is **gitignored** — the repo is public and the resume carries real contact
details. Same for `base/profile.md` and `base/applicant.md`
(`base/applicant.example.md` is the template; the `## Facts` section is what
the screen reads. Missing, the screen uses facts derived from the resume,
which now include a relocation line, and the banner says so).

## Things that will bite you

Every one of these cost a debugging cycle. They are in PLAN.md in more detail.

- **Every person keeps their own LaTeX design** (2026-09-25; the rule, not
  a preference). The tailor used to read one layout (the maintainer's
  `\texorpdfstring` headings and `\resumeItem`), and any other resume
  failed all four attempts as "modified PREAMBLE". `tailor/structure.py`
  now reads any layout: headings are commands with "section" in the name
  (or, with fewer than two, a known heading word alone on its line, like
  `\textbf{EXPERIENCE}`), `ROLES` maps "Work Experience" / "Profile" /
  "Skills" to the rules' four names, bullets are `\resumeItem` when the
  resume uses it, else `\item`. The maintainer's resume and past tailored
  ones read back identical. The prompt gets `layout_note`: the resume's
  own headings per role and its bullet form. An unrecognised heading is
  fixed in `ROLES`, never by moving a person's resume into a template.
  The resume goes in as the person's `.tex` only (onboarding upload,
  `POST /setup/resume`; never rebuilt from a PDF or Word file);
  `base/resume.template.tex` is only a start for someone with none.

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
- **Two servers, two data folders, and the queue reads empty.** A stale
  uvicorn started from the clone (no `AUTOPILOT_HOME`) keeps its data in
  the clone; a restart through `scripts/autopilot` used to force
  Application Support and showed an empty queue over 123 application
  folders still on disk. Before concluding anything was deleted:
  `pgrep -fl uvicorn`, then compare `ls ~/Library/Application\ Support/Autopilot/data`
  with the clone's. `GET /setup` prints the `home` the server is actually
  using.
- **A stale uvicorn holds port 8787** and serves old code. If behaviour makes
  no sense, `pgrep -fl uvicorn` first. Scripts it launches run current code
  regardless, so "the pipeline works but the page is wrong" means this.
  **The thread's re-tailor is the exception**: it calls `pipeline.retailor`
  in the server's own process, so it runs the `tailor/` code that process
  imported at startup. Edit `rules.md`, `layout.py` or `tailor.py` and the
  server must be restarted before Re-tailor uses any of it; `pipeline.py`
  from a terminal always runs current code. A
  button that does nothing while its endpoint answers 404 is the same thing
  (2026-09-23: the thread's Re-tailor, against a server started before
  `server/thread.py` existed). The page is served by that process too, so a
  reload does not help; restart it.
- **Something else can hold 8787 too.** Caveman Cloud's `caveman-proxy`
  (`~/.caveman/bin`, started by `caveman codex`, detached under launchd,
  no plist) listens on 127.0.0.1:8787 and answers everything with
  `cave_route_not_found`. `lsof -nP -iTCP:8787 -sTCP:LISTEN` names the
  holder. `AUTOPILOT_SERVER_URL` moves the injector and its injected
  script together when the server has to live elsewhere.
- **A re-tailor is invisible behind a cached PDF.** The new folder is
  served under the same `/review/<id>/file/resume.pdf`, so Chrome's PDF
  viewer kept showing the render it already had: the thread said
  "Re-tailored" over the old resume, and it read as "the documents did
  not change" (2026-09-24). The iframe src now carries the folder name as
  a version (`?v=<folder>`, `stamp` in `show()`), and `review.artifact`
  answers `Cache-Control: no-store`.
- **A button that needs text says so, or it reads as broken.** The
  thread's Re-tailor returned at `if (!text) return` with an empty box:
  three clicks, nothing, no message (2026-09-24). Answer it and Re-tailor
  with this are disabled while the box is empty; **Re-tailor with Opus is
  not**, because "run it again on the stronger model" is a whole request
  on its own and sends a fixed instruction instead.
- **The injector runs the code it was started with.** Edits to
  `browser/inject.py` or `browser/autofill.py` do nothing until
  `browser.inject` is restarted; a tab already open gets the script on
  attach but no load event, so the press happens on the next tab or a
  reload.
- **Importing AppKit in a process with no window-server session aborts
  it.** `browser_use/browser/profile.py` and `screeninfo` import AppKit;
  under macOS 27 a Python started by an agent harness (Codex) died with
  `_RegisterApplication ... abort()` five times in twenty minutes. Our
  scripts started from Herdr or uvicorn are fine. Not ours to fix; know
  the signature.
- **Rejecting a `filling` job stops its fill** (fixed 2026-09-30). It used
  not to: `apply.py` kept running on a rejected job and, with no form to work
  on, wandered into another job's tab - and held the serial queue behind it.
  `review.reject` kills the fill and closes its tab.
- **`el.click()` on Jobright's opener does not fill.** "Autofill my
  application" opens the panel; the control inside starts the fill. The
  press now takes the second step. A tab froze hard once during this
  (renderer stopped answering `Runtime.evaluate`, even `Page.navigate`
  was accepted and ignored); only closing the tab helps.
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
- **A form question is not a requirement.** "Will you now or in the future
  require sponsorship?" is on half the application forms in the country and
  says nothing about the employer's policy; the model made it a hard reject
  four times. `screening.is_question` passes over a match whose sentence is
  one, and `sentence_around` keeps `i.e.` and `U.S.` inside the sentence so
  the question mark is still there to be seen. A new false positive belongs
  in that rule's `unless`, with the posting's own words in a test.
- **A stretch is not a reject.** Every US city, cohorts a year off and 1.5
  years against "0-1" were all rejected once. The three are now code: the
  location policy, the graduation window, and `experience_years` reading
  the *floor* of a range.
- **Two screen policies are code invariants.** Every US location is green,
  not caution; a degree earned before an "earned or expected by" deadline
  satisfies it, and (2026-09-24) so does a degree earned before a **cohort
  window** ("spring/summer of 2027 college graduates" rejected a December
  2026 graduate; graduating early only widens what fits).
  `screen.graduation_window` reads the window's end off the quote and
  `graduating_in_time` drops the flag; a posting that wants a graduation *no
  earlier* than a date, or the applicant still enrolled, keeps it
  (`NOT_BEFORE`, and `screening._earliest` is the other half of it).
  `enforce_location_policy` and `enforce_timeline_policy` still run after
  the rules and before `verdict_for`, so a rule someone writes cannot undo
  them. Update their regression tests when changing either. Cached US
  location flags are normalized on read.
- **The injector's browser socket drops without a close frame** (once,
  30 s after a new tab). It used to die there and every tab lost its
  banner silently. `Injector.run` reconnects; if the banner is missing,
  `pgrep -fl browser.inject` and read its pane first.
- **`window.open` from an evaluated script is at the site's mercy.**
  Ashby swallowed it. Tabs the page wants are opened by the injector. The
  bridge reuses and focuses an existing Autopilot tab before creating one;
  do not regress `/__open` to unconditional `Target.createTarget`.
- **Port 8787 may belong to another local service.** Run uvicorn elsewhere
  and set `AUTOPILOT_SERVER_URL` for the injector; it rewrites the injected
  content script to the same origin. A mismatched origin splits screens and
  captures across two servers and looks like stale state.
- **Chrome keeps the extension's old `content.js` until you reload it** at
  `chrome://extensions`. The Jobright-only guard was in the file and the
  Jobright URL still got queued 40 minutes later. The server now refuses
  `jobright.ai` at `/capture`; client-side guards are convenience only.
- **Greenhouse's embedded board has one path for every job.** `/embed/job_app?for=<company>&token=<id>`: `seen.canonical` dropped the query, every embed was "the same page" as the last one, the banner said "already in autopilot", and the new job was never queued. That looked like "autofill finished and nothing started". `for` and `token` are kept and the token is the ATS id.
- **Auto-queue is off on Jobright's own pages** so one job is not queued
  under the Jobright URL and the employer URL; there the countdown presses
  Apply instead. If a job shows up twice, look there first.
- **Jobright's Apply tab has no opener.** Jobright's extension creates it,
  so `openerId` and `Page.windowOpen` are both absent. `inject.py` goes by
  the `?jr_id=` tag on the URL.
- **A page's `fetch` to 8787 can hang forever.** Oracle's ATS service
  worker swallowed it; the banner sat on "Screening" with the server idle.
  Injected `content.js` never fetches: the CDP binding does the HTTP.
- **Injection at `load` is before client-side render.** Oracle had 200
  characters at load; `content.js` now waits for text (up to 20 s) instead
  of giving up.
- **`.bar` was two things.** The banner and the button countdown shared a
  class name inside one shadow root; the countdown became `position:
  fixed` on top of its own label. Button fills are `.fill`.
- **Chrome lives on port 9333**, detached, reused across runs. If a fill
  attaches to the wrong thing, `curl 127.0.0.1:9333/json` shows its tabs.
- **Extra tabs mean extra fills.** Each `apply.py` opens its own tab. Four
  tabs on one job meant four runs; `pgrep -fl apply.py` before anything else.
- **`.env` has `AUTOPILOT_AUTOFILL=1`, so a test that approves a job launches
  the real `apply.py`** unless the fixture forces it off. `test_review.py`
  does; a stray `apply.py 3fbd` from a test run once sat in the process list.
- **The orb listens whenever the chat is open, including to you talking
  to someone else.** A dictation to Claude Code with the Profile tab up
  went in as the answer to "What level" (2026-09-21). Tap the orb
  (pause) before talking to anything but the interview.
- **A loudness gate cannot tell music from a voice.** Barge-in (`BARGE_*`
  in `Voice`) flipped replies on and off under a song in the room, and the
  turn-end detector (`SPEECH` RMS) has the same blind spot. Barge-in is off;
  the honest fix for both is a voice detector (Silero VAD in the browser),
  not a stricter threshold.
- **Fish Audio without `reference_id` picks a new voice per request.**
  With one request per sentence the reply changed speaker every sentence.
  `DEFAULT_VOICE` in `voice/fish.py` pins it.
- **A headless smoke run with a fake mic talks to the real interview.**
  `--use-fake-device-for-media-stream` fed whisper a tone, it transcribed
  `[BLANK_AUDIO]`, and the page sent that as an answer, twice, on a fresh
  profile. `stt.clean` now drops bracketed whisper markers, but do not
  point a fake mic at the live server; screenshot with static orb markup.
- **A message written in code is spoken whole unless it is labelled.** The
  opening went out as one `ask`, so the voice recited every resume bullet
  with a TTS-latency gap between them and it read as a stall. Anything the
  page will speak needs `part` / `spoken` per piece, the same as a model
  reply.
- **A slow provider looks like a dead voice.** One `/profile/turn` sat on
  an open OpenRouter stream for 4.5 minutes with no delta (2026-09-21);
  `llm.TIMEOUT` is 300 s per read, so nothing errored and the page showed
  nothing. The reply landed and was saved; it was not spoken because the
  chat had been left by then. `lsof -nP -p <uvicorn pid> | grep :443`
  tells a stalled call from a broken one.
- **The interview model skips its header sometimes.** A good question,
  no `COVERED:` line, and the answer's coverage is lost. `_stream_reply`
  returns an empty header; `_interview_turn` then grades the answer in a
  second call. Never assume the header is there.
- **The model does not stop when told to.** "Nothing more, move on" got
  another question. `MOVE_ON` in `interview.py` closes the experience in
  code; the prompt alone was not enough.
- **Two writers on `state.json`.** The children thread saves while a turn
  reads, and `write_text` truncates first: `_is_closed` read an empty file
  and the SSE stream died mid-turn. All state goes through `_write_atomic`.
- **A client that drops the SSE stream loses that question.** The
  generator is killed where it stands; the recorded transcript ends at the
  last complete save. The next turn recovers (a closed `current` advances
  the queue), but do not `head -c` a turn and expect state to be whole.
- **Bundled espeak-ng libraries are broken on Apple Silicon**, and not
  gently: the one in `espeakng-loader` (Kokoro) and the one in `piper-tts`
  both ignore their data path and the C code calls `exit(1)` on the first
  word. Probing one in the server process kills uvicorn. `voice/kokoro.py`
  only ever uses Homebrew's `libespeak-ng.dylib` (or `AUTOPILOT_ESPEAK_LIB`)
  and probes it in a child process. The Piper "macos_aarch64" tarball is
  x86_64 on top of that.
- **An `alert()` in the voice path hangs a headless check.** The page
  alerts on mic and transcription failures; a CDP-driven smoke test must
  stub `window.alert` first. Real Chrome needs `--use-fake-device-for-media-stream`
  to exercise the orb without a microphone.
- **`whisper` on PATH is not whisper.cpp.** It is the openai-whisper
  Python CLI with different flags. `voice/assets.py` looks for
  `whisper-cli` / `whisper-cpp` only.
- **Jobright's "done" never reaches the page as a DOM event off
  jobright.ai.** Their `CustomEvent("FromExtension")` is gated on
  `agentDomains`; the only signal on an employer page is the
  `window.top.postMessage` progress protocol. The listener has to be
  installed before the click or the final message is missed.
- **The logo went on the file input, and Greenhouse removes the input.**
  `MARK_FN` found no element for the ref after a successful upload and
  returned false: resume and cover letter on the slot, no logo. The host
  is now the block `MARK_UPLOAD_FN` tagged before the upload (its label
  when it has one), whether or not the input survived.
- **A filled form's tab must never be screened again.** After submit the
  tab navigates to the confirmation, `schedule()` saw a new URL and
  screened the thank-you page, painting a verdict over "submitted".
  `submissionWatched` in `content.js` stops `screen` and `schedule` for
  good in that tab; the confirmation check also wants the form gone
  (`FORM_GONE` controls), since "thank you for your interest" is in
  half the postings.
- **A snapshot pinged on submit can arrive after the navigation.** The
  tab id still resolves, the page is the confirmation, and a scan of it
  would overwrite the last good form state with a search box. `capture`
  checks the tab's host and wants ≥ 3 fields.
- **`scrollIntoView` lies under `scroll-behavior: smooth`.** It returns
  before the scroll, and the rectangle read next is where the element
  *was*: every click landed 600px off on Greenhouse. `behavior: "instant"`.
- **react-select ignores a scripted value.** Setting `.value` and firing
  `input` shows the text and opens nothing; only a real pointer or key
  event opens the menu. `Input.dispatchMouseEvent` on the control box,
  then `Input.dispatchKeyEvent` per character. The chosen value lives in a
  sibling `single-value` div and the input goes empty; read that, not
  `.value`, or the audit lists the field as still empty.
- **Greenhouse removes the file input once a file is chosen** and shows a
  filename row instead. `input.files` read back empty after a successful
  upload; the name is read off the input's block (`MARK_UPLOAD_FN`).
- **Lever's location box drops a typed value on blur** unless a suggestion
  from its own list (`.dropdown-results`, no roles) is clicked. The
  adapter's `COMBOBOX` marks it; the engine clicks "Austin, TX, USA".
- **A select-all keystroke with no input focused selects the page.** Lever's
  university picker took the click without focusing anything, `Cmd+A` went
  to the body. `type_into` checks `document.activeElement` first.
- **A generic label pattern will match a word inside a question.** "Why
  are you considering leaving your most recent or current position?" got a
  job title typed into it. Textareas and labels over 60 characters are
  answered only from `answers`.
- **A tailored resume may link only what the master links or a story's
  `Link:` line says.** `tailor._check_links` rejects any other `\href`;
  `rules.md` allows one whole `PROJECTS` entry to be swapped for a story
  that has a link. A story without `Link:` is never swapped in.
- **`tailor.llm` is one module.** A test that stubs
  `interview.llm.complete` has stubbed the GitHub scan's model too; the
  scan tests give `github.llm` a namespace of its own.
- **GitHub's `author=` filter matches the login only when the commit
  email is linked to the account.** Two of the handle's own repos came
  back with zero commits on the first live scan; an owned repo with none
  attributed is now counted whole and noted, never skipped. `409` on
  `/commits` is an empty repository.
- **`POST /settings` changes only the keys sent** (2026-09-20). It used
  to take the whole `Settings` model with defaults, so the page's
  `{use_profile: true}` would have reset `scout_checks_per_day` to 4.
  `Optional` fields, `exclude_none`.
- **Google's careers JSON is gone.** `careers.google.com/api/v3/search`,
  the endpoint every 2025 scraper used, answers `{"detail":"Not Found"}`.
  The results page under `google.com/about/careers/applications/jobs/results`
  server-renders the rows into `AF_initDataCallback({key: 'ds:1' …})`,
  positional arrays; `scout.providers._google_rows` reads them and
  raises when the block is missing, which is the red BROKEN, not a
  silent empty list. Field order was checked live 2026-09-20; when it
  drifts, that function and its test are the place.
- **Gmail refuses the account password over SMTP.** Only an App
  Password logs in (2-step verification first). `mail.send` turns the
  `SMTPAuthenticationError` into that sentence so the page says it.
- **The preamble is no longer argued about** (2026-09-24, `restore_preamble`):
  a reply whose preamble differs has the master's spliced back in before
  validation, and the attempt is judged on the sections. It used to be a
  byte-exact rejection (`_check_frozen_sections`; only whitespace runs
  collapsed), which cost Abridge two of four attempts and DoorDash one, for
  a reflowed comment no reader sees. The splice is recorded as a warning on
  the review page, since a model spending attention above
  `\begin{document}` is a prompt problem. `rules.md` still says nothing above
  `\begin{document}` changes, not even a comment, and adds that whatever is
  changed there is discarded.

## Testing

`pytest` collects 917 tests (860 without `test_fill.py`); `tests/test_invariants.py` is the short list
every copy runs after any change (`scripts/check.sh --quick`), and
`tests/conftest.py` points prompt edits and the Workshop store at a
temporary folder for every test. The suite stubs the model, browser, lualatex, and
speech binaries, so **passing tests do not mean it works** — every real bug so
far survived a green suite and appeared on the first real run.
**`tests/test_dom.py` is the exception, and exists because of that
sentence** (2026-10-06): the scripts under `browser/forms/` and `putFile` in
`capture/content.js` are JavaScript that reads somebody else's markup, and
the only honest test of that is to run it against the markup. It launches a
headless Chrome of its own (its own port, its own temporary profile —
Chrome will not share one, and the person's browser is not ours to drive)
and runs the real `SCAN_JS`, `FIND_REMOVE_FN`, `TAG_NAMED_FILE_FN` and
`MARK_FN` against the fixtures in `tests/dom/`, which are the shapes the
real forms have; `ashby_application.html` is the live Deepgram form's own
markup, class names and all. Each of the ten was checked against the code
as it was before the fix: six fail there. No Chromium = skip, so a machine
that has not run the app is not stopped by them - and `scripts/check.sh`
runs them under a heading of their own and **says the skip out loud**,
because they are the only place the resume slots, the remove step and the
logo are checked against a page, and a silent skip there is how a green
suite stops meaning anything.

**A test that reads a script's source cannot tell a rewrite from a
regression** (2026-10-06). Three were deleted the day after they were
written, for exactly that: two asserted the text of `FIND_REMOVE_FN` and of
`putFile`'s filter expression, and one asserted that a name existed in a
module (`TAG_NAMED_FILE_FN in dir(engine)`), which a mutation audit over all
seven fixes showed caught nothing at all. The Workday bug reintroduced as
`const slot = 'input[type="file"]'` rather than inline **passes** the source
check and **fails** the browser one. The source-reading tests that stay are
the ones asserting a *rule* rather than an implementation - invariant 6 (the
agent may not import the submit presser), `open_apply` not naming it - where
the text is the thing being promised. On the current
macOS / Python 3.13 environment, the full process aborts inside browser-use's
native display detection at `test_forbidden_actions_are_absent_from_the_agent`;
run the focused files plus `pytest --ignore=tests/test_fill.py` until that
dependency issue is fixed, and still exercise the changed path live.

## Next up (2026-09-20)

Agreed with the human, not built yet. Each is a flow change, so read the
invariants first.

- Projects from GitHub (2026-09-20): built. Rank puts an auto-synced
  submissions repo first (443 commits); the candidate unticks it. A
  weight against names like `submissions`/`dotfiles` is the next step if
  it keeps happening.
- Tailoring prompt strengthened (2026-09-21): built; judge on the next real runs, and again once `base/profile.md` and the stories exist. Next if asked: a code check that every new term in the tailored resume occurs in the master or a story (the paper's "deterministic verification"), on top of the number and link checks.
- **Export control is caught, and the screen is no longer a model call**
  (2026-09-21 raised, built 2026-09-26). Seven postings on the corpus
  carried ITAR, EAR or "U.S. Person Required" and the model passed every
  one. See the entry at the end of this section.
- Scout notify/autopilot split (2026-09-21): built. Next if asked: a
  weight on the code pre-screen (a "senior" in the requirements text),
  a bulk paste of company URLs, and a Notifications badge count on the
  tab button.
- Scout (2026-09-20): built. Google's `careers.google.com/api/v3/search`
  is gone; its results page embeds the rows (`AF_initDataCallback`
  `ds:1`, positional: id 0, title 1, locations 9, description 10, posted
  epoch 12, min quals 19) and `target_level=EARLY`, `location`, `q`
  carry over from the pasted URL. Next if asked: Telegram, a weight on
  the screen's verdict in the subject line, LinkedIn (no public API).
- **Hand-opened ATS tabs get the banner** (2026-09-21 raised, built
  2026-09-26). `inject.is_ats` (and the same list in `background.js`) marks
  an application form by host alone - Workday, Greenhouse, Ashby, Lever,
  SmartRecruiters, BambooHR, Workable, iCIMS, Jobvite, Taleo,
  SuccessFactors, Oracle, Eightfold, Dover - so a posting reached from a
  company's own careers page is screened like one Apply opened. Those tabs
  are deliberately **not** in `Injector.marked`: the injected script is
  prefixed with `window.__autopilotHandOpened`, and the banner then never
  counts down, never presses Autofill and may not close itself. A page
  being read is not a decision made.
- The watch overwrites `form_state.json` with an empty resume slot after
  the form's tab reloads (Ashby drops the file on reload); the review page
  then shows no resume. Raised 2026-09-21, not built: keep the last
  non-empty reading, or say "reloaded" instead.
- Per-job thread, kept-status re-tailor and Finder reveal (2026-09-23): built, and DoorDash (`588d`) and Abridge (`ee29`) re-tailored through it with the facts and stories in place. Next if asked: the thread on the Scout tab's rows, and a
  "re-tailor everything still in flight" sweep.
- **Where things stand (2026-09-24).** DoorDash (`588d`) and Abridge
  (`ee29`) are the two jobs in flight, both `awaiting_review` in
  `applications/2026-09-24_*`, both re-tailored from scratch under the
  rules below with the facts, stories and GitHub projects on file, both
  with `auto_fill` off on the row so neither approves itself. Their older
  folders were deleted at the human's request; the links are what was
  kept. Raised and not built: splice the model's four editable sections
  into the original preamble in code instead of rejecting a reply that
  touched it — Abridge spent two of its four attempts on
  `modified PREAMBLE`.
- Tailoring opened up (2026-09-24, after "one project changed and nothing
  else" on a filled-in profile): stories may swap projects, replace a bullet
  under the same role and trade a skill; every mapped bullet is rewritten to
  X-Y-Z; length is printed lines (`tailor/layout.py`), not characters, for
  bullets, the summary and each skills line; a rejected attempt is told to
  keep its other edits (it used to revert to the master, which is what made
  the DoorDash run look empty); the rationale lists what the checker
  rejected. Next if asked: a code check that every term added to the resume
  occurs in the master or a story, and a count of how many bullets a run
  actually changed.
- **Six models measured on one posting (2026-09-24, Abridge `ee29`,
  identical prompt).** Diff lines / EXPERIENCE bullets rewritten /
  attempts: opus-5.5 34 / 5 of 6 / 2; qwen3.7-max 30 / 5 / 2;
  glm-5.3 14 / 1 / 2; kimi-k2-thinking 14 / 0 / 3; minimax-m3 14 / 0 / 1;
  sonnet-5 12 / 0 / 1. Cost per 100 jobs on the measured prompt (11.2k in,
  3.0k out, 2 attempts): minimax $1.40, kimi $2.85, glm $3.47, qwen $5.97,
  sonnet $10.51, opus $21.02. The cheap field all does the same thing —
  swap a project, reorder a skills line, leave the bullets alone. Qwen
  matched Opus on volume by inventing framing ("LLM-adjacent NLP
  workflows" for a grammar service), which is worse than doing nothing on
  a document the person signs. Sonnet is not worth its price here. Hence
  the button rather than a new default: glm-5.3 stays, Opus is a click.
  The folders are `applications/2026-09-24_abridge_..._v4` (glm) through
  `_v10` (minimax) if the comparison is worth re-reading.
- One caution on the Opus output: it writes numbers the checker cannot
  verify (the sycophancy entry's falsification gaps, Auto-Apply's
  "100+ jobs in a week"). Links and counts are checked; figures are not.
  Read them before a job goes out.
- **Shared through Claude Code (2026-09-24).** INSTALL.md / UPDATING.md,
  `scripts/`, `paths.py`, the Prompts tab and its Workshop, learning from
  re-tailor requests, Tectonic, no browser-use. Not built: the Workshop's
  before/after preview on past jobs, the Workshop by voice, a packaged
  and notarised app (Electron or Tauri) if friends without Claude Code
  want it, the in-app bug report. PLAN.md Phase 19.
- Early graduation is no longer a reject (2026-09-24): SeatGeek's
  "spring/summer of 2027 college graduates" rejected a December 2026
  graduate. Cohort windows are now parsed like cutoffs in
  `screen.graduation_window`, the rules say graduating early is a match, and
  the four cached rejects were repaired. Next if asked: the same deterministic
  treatment for export control, which is still prompt-only.
- **Approving a poor fit now tailors it** (2026-09-25). A MISMATCH run writes
  `mismatch.md` and no documents, so approve (and `/fill`) launched `apply.py`
  against an empty folder: it skipped with "resume.pdf is missing" and the job
  sat at `approved` with nothing to show (Scribe `b37f`). `review._tailored`
  checks for `resume.pdf`; missing, `_overrule_mismatch` runs
  `pipeline.retailor(..., keep_status=True)` with the `OVERRULE` instruction in
  the job's own thread (so the page can watch it) and starts the fill when the
  documents land. Nothing about never-submit changes.
- **The list is newest first everywhere** (2026-09-25). The closed shelves
  showed the queue's own order, oldest first, so the newest submission and the
  newest failure sat at the bottom of a collapsed shelf and read as gone (a
  failed Adobe run did). `NEWEST` in `review/index.html` sorts both shelves and
  breaks ties inside the in-flight order.
- **The folder is named after the employer even when nothing reports one**
  (2026-09-25, `pipeline.company_from`): Greenhouse's embed exposes no company,
  so fifteen folders were `unknown-company_...` while the row's own title said
  "... at SeatGeek" and the URL said `for=seatgeek`. Title patterns first
  ("at X", "Role @ X", "|X|"), then the ATS slug in the host or path. Existing
  folder names are immutable and were left; the rows were backfilled.
- **A fact asked with a question mark is not an open question** (2026-09-25,
  `forms.engine.describes_fact`). "What is your legal first name?" and
  "Preferred pronouns?" went to the tailor model because of the `?`, which
  rewrote what Jobright had right and answered pronouns with three sentences
  about being happy to share them. Names, pronouns, contacts, places, links,
  schooling, salary and start dates are facts (`FACT_LABEL`); "why", "describe"
  and "tell us" keep a label prosaic (`PROSE_LABEL`). A single-line box also
  gets one sentence at most (`Engine.one_line`), since the model writes two to
  four by the rules.
- **The logo marks survive the page redrawing itself** (2026-09-25). Ashby's
  form is one tab of a React page: leaving for Overview and coming back
  unmounted the form, every logo went with it, and it read as "did the resume
  come off too?" (it did not; a live snapshot showed the file and the answers
  still there). `MARK_FN` keeps each mark in `window.__autopilotMarks` with its
  label's text and one MutationObserver puts back whatever goes missing; a
  remount has no `data-autopilot-ref`, so the label text is the second anchor.
- **The badge is purple while anything is filling, green when it is done**
  (2026-09-25). Jobright's own autofill is the longest visible part of a run
  and said nothing, so the core only turned purple once our step started;
  `autofill.run` now signals `working` right after the press. The end of a fill
  says "Filled and ready; your check, then Submit" and the core goes green
  (`.shell.done`), which is the whole meaning of checkpoint 2.
- **Tailoring volume is enforced, and invention is checked** (2026-09-25).
  A change floor (half the `EXPERIENCE` bullets, soft on the last attempt)
  and a figure/name check against the master, the stories and the posting;
  both listed in `rules.md`, since a budget the prompt does not name is one
  the model fails blind. Next if asked: a count of bullets actually changed
  on the review page, and the same treatment for the cover letter.
- **A job already applied for is shouted at screening time** (2026-09-25).
  `ALREADY APPLIED` in red on the banner for `submitted` / `filled` /
  `filling`, and that bar does not fold into the badge. Next if asked: the
  same block on the review page's own list row.
- **106 tailored resumes measured, and the three things it found fixed**
  (2026-09-25). Over every folder on disk, reverse-applying each
  `resume.diff` (the master never drifted): 199 of 636 `EXPERIENCE` bullets
  changed, but 110 of those are one-term swaps and only 22 are real rewrites;
  the summary changed in 101 runs, 66 runs reordered the skills lines without
  trading a term; 726 characters of line budget sat unused (net characters
  added, median -2). 51 rejections were a line budget, and the model retreats
  to the smallest passing edit after one. Projects were swapped in 26 runs, 18
  of 95 jobs - never before the stories existed, and **swaps track the size of
  the pool, not the prompt**: a pool of four candidates produced two swaps in
  five runs of six, a pool of one produced none in nine of ten, and
  `profile.pick` was spending two or three of its four slots on stories for
  what the resume already carries. Hence the split slots, the pooled block
  arithmetic, the room counted down, and the swap floor. Next if asked: the
  same treatment for the cover letter, and a count of bullets actually changed
  on the review page.
- **A poor fit is tailored anyway** (2026-09-26). `tailor.Mismatch` used to
  take an early return in `pipeline.process`: `mismatch.md`, no documents,
  `awaiting_review` - and, because the return was before `auto_approve`,
  `auto_fill` could never carry the job however it was set, so every poor
  fit cost a click before anything existed to read. The verdict is kept and
  shown, then the tailor is called once more with `tailor.OVERRULE_QUEUED`
  ("the candidate chose this posting themselves ... never invent the thing
  they lack") and the job travels the rest of the pipeline like any other.
  Refused on that call too = the old behaviour, documents and all, and
  Approve still overrules from the page (`review._overrule_mismatch`, whose
  `OVERRULE` now lives beside it in `tailor/tailor.py`). Still prompt-only
  on the other side: `screen.graduation_window` fixes "the 2027 cohort" for
  a December 2026 graduate in code, the tailor has no such pass, so it calls
  that a hard filter and costs the second call. Next if asked: give the
  tailor the same deterministic treatment.
- **The put chips are offered on any match** (2026-09-26). `loadFiles` ran
  only at `seen.level === "high"`, which is the wrong half: a form the
  person opened by hand is exactly where "put" is wanted and is usually
  `confident`. Nothing goes on the form without the click.
- **Onboarding is finished once, for good** (2026-09-26). `settings.save`
  cannot unset `onboarded`, and the flag is written twice: `settings.json`
  and a `.onboarded` file beside it (`settings.onboarded_mark()`, resolved
  per call, never frozen at import). Either copy says yes and the wizard
  stays away. A person weeks into the app was shown the first screen again,
  which reads as the app having lost their work.
- **A checkout keeps its own data** (2026-09-26). `scripts/autopilot`
  exported `AUTOPILOT_HOME` unconditionally, so the maintainer's clone -
  105 queue rows and 123 application folders in `data/` and
  `applications/` - was pointed at an empty Application Support folder and
  the queue read empty. Nothing was lost and nothing was moved: the script
  now takes the clone's own data when it has a `data/queue.json` and the
  app folder has none. `run/` and `logs/` are gitignored for the same
  reason.
- **Screening is code, and it has its own tab** (2026-09-26). The model is
  retired. Measured first: every one of the 122 postings on disk replayed
  through the rules and compared to the 199 verdicts the model had cached,
  and all thirteen disagreements went to the rules - **seven** hard
  export-control blocks it had passed (ITAR, EAR, "U.S. Person Required":
  ASM, Sift Stack, Axon, three more) and **six** rejects it should never
  have raised (a form's own sponsorship question three times, "100%
  onsite" in a US city three times). Its wider cache held worse: `Will
  sponsor`, `United States (remote)`, `$30/hr`, `NO RECRUITERS, PLEASE.`,
  `E-Verify` and `0-1 years of experience` were all hard flags. Twelve
  stock rules, each a checkbox, in `tailor/screening.py`; the person's
  switches, thresholds and own rules in `data/screening.json`; a
  **Screening tab** to change them and an onboarding step that shows them
  once. Its assistant writes a *rule* from a sentence ("flag anything that
  wants a clearance") - one model call per rule written, never per job -
  and every proposal is checked (the patterns must compile and must not
  match ordinary prose) and previewed against the postings already on
  disk before it can be added. The old verdicts in `data/screens.json`
  were left alone by decision: rules from now on. Next if asked: a
  "re-screen everything" sweep, and the same rules in
  `scout.filter.prescreen`, which still keeps its own smaller list.
- **ALREADY APPLIED is the way back to what was sent** (2026-09-26). The red
  block on the posting page is now the button (`data-act="open"`, Enter and
  Space too, "Show me what I sent →"): it opens that job in the Autopilot
  tab, focused. It says the time to the minute in the reader's own zone
  (`appliedWhen`), not just a date - two applications can go out on one
  day - and the review page answers it with a banner of its own
  (`reachedAt` over the status history): "You submitted this application ·
  Thu 17 Sept, 17:19", above the resume and letter that went with it.
  Saying "already applied" and leaving someone to go and find it is the
  same as not saying it.
- **The Screening tab is one pane, and she speaks** (2026-09-26). The left
  column repeating every rule's name beside the rules themselves was the
  same information twice; `main.nonav` gives the tab the whole width and a
  rule's patterns fold away under "why". The assistant sits at the top with
  the orb: opening the tab she says "Hey - what kind of screening would you
  like to add?", speaks it and listens, and the conversation is a
  transcript rather than a form. `Voice` now talks to a **host** rather
  than to `Profile` by name (`Voice.setHost`, `who()`), and the orb's DOM
  (`orbHtml`, `placeOrb`, `dragOrb`, the tap callback) is borrowed from
  `Profile` rather than copied; a host implements `orb`, `caption`, `send`,
  `idle`, `speechDone`, `paused` and `voice`. Only one chat is on screen at
  a time, which is what makes one orb enough.
- **A decided job says its outcome, not where it is kept** (2026-09-26).
  "Already in autopilot · Rejected" led with the plumbing and buried the
  answer. On a posting opened a second time the banner now says `APPLIED`,
  `REJECTED — <reason>` or `FAILED` with the time, and that line is a
  button to the job (`DECIDED` and `openLabel` in `content.js`, whose
  buttons promise what the person wants next: "Show me what I sent", "Show
  me why I rejected it", "Show me what happened"). The reject reason
  travels on `seen.Match.reject` from the queue row's `reject_reason`.
  Only a job still in flight reads "In autopilot · Waiting for your
  review", because there the progress *is* the answer. At `confident` the
  outcome is prefixed with `LOOKS LIKE THE SAME JOB ·`, since it may be
  another role at the same employer. The loud block and the line never
  both appear: the same fact twice reads as two facts.
- **A form behind a sign-in is not a failed job** (2026-09-26). Workday,
  McKinsey, CVS and eleven others keep the application behind Apply and a
  login, and the fill never presses Apply. It marked the job `failed`, and
  a failed job sits in a collapsed shelf, so **fourteen** applications with
  perfectly good tailored resumes read as gone (Morgan Stanley was the one
  that made this visible). `FillResult.no_form` now takes the job back to
  `awaiting_review` with `needs_sign_in.txt` and a banner saying what to do
  - sign in until the form is on the screen, then Approve. The fourteen
  were brought back. **And the gate that recognises it was wrong**: it
  wanted fewer than five fields, and Workday's sign-in page has six, so
  the fill treated a login screen as the application, found no resume
  slot, and wrote a tailored sentence into a hidden box labelled "This
  input is for robots only" - a spam trap. No file input anywhere is now
  enough (`NOT_A_FORM`), and `engine.HONEYPOT` skips such a box in both
  the answerer and the generic label matcher.
- **The submission watch silenced a tab for good** (2026-09-26). It exists
  to stop the banner painting a verdict over the page a submitted form
  turns into, and it is restored from `sessionStorage` on every page of
  that tab. So a Workday tab that had once held a fill was signed into,
  navigated to the real application form, and got no banner at all - no
  verdict, no put chips, no way to attach the tailored resume.
  `notTheConfirmation()` limits the suppression to a page that actually
  reads as a confirmation; the stored job id and the poll stay, so a
  confirmation reached later is still marked, and `/screen` answering
  `submitted` for one in string work is the second guard.
- **Submit from the review page** (2026-09-26, asked for). A filled job's
  fab now reads Reject / Fill again / I submitted it / **Submit it**; the
  last presses Submit on the form's own tab over CDP after one confirm
  dialog naming the job, and marks the job only on a confirmation page.
  The never-submit rule is unchanged for the agent and is now checked from
  the other side too (invariant 6: nothing the agent runs may import
  `browser/press_submit.py`, and only `server/review.py` calls it). Next if
  asked: the same button on the list row, and a re-press when a form comes
  back with a validation error.
- **One tab at a time, and a switch that holds the line** (2026-09-27,
  asked for). Adding six jobs while scrolling a list started six fills, six
  Chrome tabs in the one window the app owns, and nobody checks six forms at
  once. `settings.hold_fills` (**on by default**) is a switch at the top of
  the review page, worded from the person's side rather than the code's -
  `Collecting jobs · N ready`, then `Starting applications…` for the few
  seconds before the first tab, then `Applying · N more in queue`: held,
  the pipeline runs in full - screen, tailor, compile, cover letter,
  `auto_approve` - and every job stops at APPROVED. Letting go plays one
  beat (`liftoff` in `review/index.html`, 1.5 s, "Taking flight" over the
  assistant's own ring rising, a green track filling,
  `prefers-reduced-motion` honoured): the click changes what the machine is
  *for*, from gathering jobs to applying for them, which is more than a
  label quietly changing in a corner. Going back to collecting is silent.
  Let go, `runner.serial_tick` (a 3 s thread, `runner.start_serial`,
  `AUTOPILOT_SERIAL=0` off) starts the oldest approved job when nothing is
  filling, so each job gets one tab and the next begins when that process
  ends, whether it reached checkpoint 2, failed, or stopped for the person.
  `runner.start_fill` is the gate (`force=True` is a human pressing "Fill
  the form" on one job, which is not the bulk the hold is for), and `/fill`
  now refuses while *another* job is filling unless forced. Nothing about
  never-submit changes: the line ends at the filled form.
- **The banner says what is true, once, and gets out of the way**
  (2026-09-27). Three things were wrong with the block on a posting already
  in autopilot. It shouted `ALREADY APPLIED` over a `filled` job, which is
  the tailored form waiting in a tab for the person's own Submit - the one
  thing that had not happened - so the heading is now per status
  (`ALREADY APPLIED` red for `submitted`, `ALREADY IN AUTOPILOT` and
  `FILLING THIS NOW` amber for the other two, `.applied.pending`). It said
  the same thing five ways (what, when, company, "Same posting.", an
  underlined "Show me what I sent →") *and* the bar carried a second button
  saying it again, in a boxed panel a third of the bar high: it is one flex
  row now, heading plus one line, the block itself is the button, the
  promise is in its `title`, and the bar's own open button is suppressed
  while it shows. And **every banner folds into the badge after 3 s**
  (`AUTO_COLLAPSE_MS`, was 5 s and gated to `seen`/`submitted` pages with
  the applied block held open for good): the two that do not fold are a
  screen still running and a countdown about to queue. **Expanding the
  badge starts the same three seconds again** - opening it is a look, not a
  decision to keep the bar - and hovering or tabbing into the bar holds the
  countdown (`stopCollapse`, restarted on `mouseleave` / `focusout`), so it
  only ever takes back a bar nobody is reading. That reverses the
  2026-09-25 rule that the applied bar never collapses; the reason it was
  written (a *line inside* a bar that folded away) no longer describes it.
- **"Already applied" pointed at the wrong row, and dated it wrong**
  (2026-09-27). Several of our rows match one posting - the Jobright page
  and the employer page, an application sent last week and a duplicate
  added this morning - and `seen.find` sorted by `(employer page,
  not-rejected)`, so the two tied and the in-flight duplicate won. The
  person is asking what happened with this job, and the answer is the row
  that got furthest: `seen.APPLIED_FIRST` / `_best` rank submitted →
  filled → filling → the rest, at both `high` and `confident` (the
  confident branch used to return whichever row the loop reached first, not
  the best). And `Match.at` was `added_at`, when the posting was *noticed*:
  `store.reached_at(folder, status)` + `Known.decided_at` give the banner
  the time it was actually submitted or rejected, which is also what a
  rejection's 90-day TTL should age from. `/jobs` carries `decided_at` and
  every row on the closed shelves shows it on the right, replacing a
  "done" / "stop" label that only repeated the shelf's own name.
- **"I submitted it" works before the fill ever ran** (2026-09-27). Someone
  who opened the posting and applied on the employer's own site still had
  the job sitting in flight here, and the button refused because the
  pipeline had not reached FILLED. It records what the person did, so it is
  accepted from any live status and refused only on a job already submitted
  or rejected; it is on the reviewable fab as well as the filled one. The
  automatic paths (`mark_seen`, the watch, `/submitted-seen`) still want
  FILLED or FILLING, because there a stray "thank you" is the risk.
- **The sign-in is watched, not reported** (2026-09-27, asked for, and the
  other half of the 2026-09-26 sign-in entry). A form behind an account
  stopped the run: `needs_sign_in.txt`, back to checkpoint 1, sign in, come
  back to the review page, press Fill again. The documents were always
  fine; the round trip existed because nobody was watching the tab.
  `browser/signin.py` takes the flow apart into two detectors that never
  consult each other - `looks_like_signin` (a password box, an identity
  host, or a sign-in phrase **that is the whole line**, since "users log in
  to manage their orders" is in half the job descriptions in the country;
  `ALWAYS` / `ON_ITS_OWN`, and a resume slot outranks any header) and
  `looks_like_form` (somewhere to put the resume, or `FORM_FIELDS` real
  fields, never on a provider's host). They are joined only by
  `wait_for_form`, which follows **the tab, not the link**: single sign-on
  leaves the employer entirely (`IDENTITY_HOSTS`: Google, Microsoft, Apple,
  Okta, Auth0, `id.workday.com`) and comes back, and the CDP target id is
  the one thing that holds still through it. `signin.ready_tab` reuses the
  tab already open on the job rather than opening a second beside a
  half-finished sign-in, and `autofill.in_tab` presses in that exact tab,
  because matching on the URL again would lose it. The fill then runs from
  the **first page** as though the wall had never been there - page 1
  autofilled, Continue, the resume swapped on page 2. Nothing is typed,
  clicked or read: the credential is the person's. `SIGNIN_TIMEOUT` 600 s,
  the banner says "Waiting while you sign in - I will carry on from the
  first page", and `AUTOPILOT_WAIT_FOR_SIGNIN=0` restores the old
  hand-back. Next: `browser/workday.py`'s page walk on top of it.
- **"Use Opus" stops and hands the prompt over instead of spending**
  (2026-09-27, asked for). Opus rewrites 5 of 6 EXPERIENCE bullets where the
  cheap field rewrites 1, at about twenty times the cost per job - but the
  person is already paying for Claude, and pasting a prompt into a chat they
  have open costs nothing. `llm.premium_model()` now resolves to
  `byhand.BY_HAND` (`settings.opus_by_hand`, default on; off restores
  `OPENROUTER_PREMIUM_MODEL`), so the banner's green button, the `jr_id`
  pref and the thread's "Re-tailor with Opus" all mean the same thing in one
  place. `pipeline.process` screens, fetches, picks the stories, builds the
  prompt and stops: `handoff.md` in the folder, `awaiting_review`, no
  documents, no `auto_approve`. The review page shows a card - **Copy the
  prompt** (`GET /review/{id}/handoff`, fetched only on the click: it is the
  whole master resume and the posting) and a box for the LaTeX
  (`POST`, `byhand.accept`). **The prompt is built by the API path's own
  code** (`tailor.system_prompt` + `_build_user_message`, plus one `HEADER`
  paragraph, since a chat has one box where the API has two) and **the reply
  meets the API path's own checkers** - sections, preamble splice, links,
  counts, printed lines, invented figures and names. The two soft floors
  (`under_tailored`, `unused_swap`) are warnings rather than rejections
  here, because the person is the retry loop and has already spent more than
  an attempt; a refusal comes back as words with their paste still in the
  box. Accepted, `pipeline.finish` (split out of `process` for exactly this)
  writes the documents, compiles, writes the cover letter and lands the job
  at checkpoint 1 like any other, and `handoff.md` is deleted.
  `fetch.Posting.from_markdown` + `pipeline.read_posting` bring the posting
  back off disk, since the answer arrives hours later and a re-fetch would
  be a different posting.
- **The badge is on every page, cold** (2026-09-29, asked for). A posting can
  be on any host - a company's own careers page, a startup's one-off form -
  so the host list (`is_ats`) could only ever guess, and the pages it missed
  got no banner, no verdict and no way to put the tailored resume on a slot.
  Every http(s) page now gets `content.js`; the app's own page is the only
  exception. A page nothing pointed at a job is **cold**
  (`window.__autopilotCold`, set beside `__autopilotHandOpened` by
  `Injector.cold` and by `background.js`): `schedule()` calls `renderCold()`
  instead of `screen()`, which draws the orb at rest in the corner and makes
  **no request and no read of the page at all** - not `/screen`, not
  `pageText()`. The click is the whole decision; from there the page is a
  hand-opened one, so it screens, offers, and never counts down, never
  presses Autofill and never closes itself. A cold page that turns out to be
  a job already on file gets what any match gets: the outcome line and the
  put chips for that job's own resume and cover letter. **The click does
  count down** (`askedByHand`, 2026-09-29): cold sets `__autopilotHandOpened`
  so the banner cannot act on its own, but the person clicking the badge is
  the decision that flag exists to wait for, so an `ok` or `caution` verdict
  queues itself from there like any employer tab. The tab is never closed
  after it, though - the posting is the page they are standing on. This is what makes
  "the badge on every page" safe to say about a browser that also holds
  email: the badge is drawn locally and reads nothing until it is asked.
- **Reject stops the work, and it is never taken away** (2026-09-30, asked
  for, and the end of the "rejecting a `filling` job does not stop its fill"
  entry below). `review.reject` now calls `runner.stop_fill` and closes that
  job's form tab (`chrome.close_tab(_form_tab(...))`, the same one line a
  submission uses), records the killed pid in the status note, and returns
  `killed` / `tab`. Nine approved jobs stood still for twenty minutes behind
  one job the person had already rejected, because its `apply.py` kept the
  one tab the app owns and `serial_tick` waits for that. And the button was
  missing exactly when it was wanted: `fillWorking` hid every option during
  the fill's first grace seconds, so the "Agent is working" row had no way
  out. That row carries Reject now; the `.actions` row already carried it
  everywhere else.
- **`pgrep -af` is not a thing on macOS, and it silently disabled the
  one-tab rule** (2026-09-30). `runner.any_fill_running` parsed
  `pgrep -af apply.py` for "<pid> <python> <path> <job id>"; macOS pgrep has
  no `-a`, printed bare pids, nothing matched, and the answer was always
  None. The guard therefore held only through `_fills`, this process's own
  memory, so every server restart forgot what was filling: three `apply.py`
  runs opened tabs on one Chrome window within a minute of a restart. It is
  `pgrep -fl` now, and output in an unexpected shape answers `"unknown"`
  rather than None - saying "nothing is running" is the one answer that
  opens a second tab. The five tests that covered `/fill` were passing
  *because* of the bug (they read the maintainer's real process list);
  `tests/conftest.py` stubs the scan for every test but the one marked
  `real_fill_scan`.
- **Apply is pressed, so the account can be offered** (2026-09-30, asked for:
  "fix Workday"). `browser/open_apply.py`, called from `signin.wait_for_form`.
  A Workday job opened its tab, found the posting rather than a form, and
  waited the full `SIGNIN_TIMEOUT` for a sign-in that **cannot appear until
  Apply is pressed** - ten minutes of the one-at-a-time fill queue on a page
  where nothing was ever going to happen (Globus Medical, `3dcc`). The
  principle it was protecting is intact, because Apply is not Submit and the
  difference is checked on the page: `safe_to_press` wants no file input,
  fewer than `signin.FORM_FIELDS` editable fields and no password box - a
  posting, with nothing on it that could be sent - and `APPLY_START` must
  match the control's whole label while `NOT_APPLY` (submit / send / finish /
  withdraw / save / sign in) disqualifies it. At most `MAX_PRESSES` (3), which
  is posting → Workday's chooser → "Apply Manually". The module does not
  import `guard`, may not even name the submit presser (invariant #6 reads it
  as text), and has one gated `Input.dispatchMouseEvent` in it; invariant
  "the apply press can never be a submit" checks both halves. It is not
  Workday-specific - ADP, iCIMS, Rippling and the rest word their button the
  same way.
- **The long wait belongs to a wall that is really there** (2026-09-30).
  `signin.NO_SIGNIN_TIMEOUT` (90 s) is what a page gets when no sign-in was
  ever detected; the generous `SIGNIN_TIMEOUT` (600 s) applies only once
  `saw_signin`. Somebody halfway through creating an account deserves ten
  minutes; a posting nobody is standing in front of was costing the fill
  queue the same ten.
- **A dropped CDP connection is not a failed application** (2026-09-30).
  Two of the three Greenhouse jobs on the failed shelf died on the socket -
  `no close frame received or sent`, `Session with given id not found` - with
  the tab sitting there fillable and the tailored resume beside it.
  `fill._press_again` retries `autofill.press` once, on a `TRANSPORT` error
  only: a page with no Autofill control will not grow one in two seconds.
- **Workday, page by page** (2026-09-30, asked for). `workday.walk` is the
  driver the module was missing: per page, Jobright's autofill, then the
  tailored documents on the page that has a slot (and only until they are
  on), the corrections and the open questions, then that page's own "Save
  and Continue"; up to `MAX_PAGES`. It ends at the review page - Submit is
  never pressed - and stops and says whose turn it is (`PAUSES`) on an
  account, an email to verify, a question Workday will not pass
  (`blocking()`), or a page that would not move. `fill_async` runs it in
  place of the one-page path when `workday.is_workday(url)`;
  `AUTOPILOT_WORKDAY=0` goes back. It writes `workday.json` beside the
  screenshot. Two bugs its tests found: `stage_of` called a **posting** a
  review page (`guard.describes_submit` answers yes to "Apply", since for
  the agent starting an application and sending one are equally forbidden),
  so the walk would have stopped on the posting believing it had finished;
  and `advance`'s timeout was frozen into a default argument at import,
  which is the trap written down twice already in this file.
- **A posting with "Sign In" in its header is not a wall** (2026-09-30,
  found live, not in a test). Every Workday tenant's header carries one, and
  Globus Medical's posting had no fields at all, so `ON_ITS_OWN` matched the
  whole line and `looks_like_signin` said yes - the watch then waited for
  someone to get through a wall that was not there while the Apply button on
  the same page was never pressed. `signin.LOOK_JS` now counts the controls
  matching `open_apply.APPLY_START` (one pattern, both places) and a page
  offering to start an application is not a sign-in. A password box is still
  decisive, so the account page behind Apply is still a wall.
- **A job waiting on an Opus paste sorts to the bottom** (2026-09-30, asked
  for). `/jobs` carries `handoff` (`handoff.md` in the folder) and the
  in-flight list breaks ties on it: the job is waiting on the person in a
  chat somewhere else, which may be hours, so it is not the one to open
  first. The row says "waiting for your Opus paste".
- **A job can be taken back out** (2026-09-30, asked for). The countdown now
  reads "Adding to autopilot · click to stop", and once it has been added the
  same button becomes "Undo · take it out again": `POST
  /review/{id}/reject` with `changed_my_mind`, so the folder and the record
  stay. `runner.stop_pipeline` goes with it, because a job cancelled seconds
  after it was added is usually still being tailored.
- **A dialog is not in the pane, so `act` must not disable it** (2026-09-30).
  `act` greys every button while a decision is in flight and restored them
  only when the call *threw*, on the grounds that a success re-renders the
  pane. `querySelectorAll("button.act")` reaches the reject and report
  dialogs, which live outside `#detail` and which `show()` never redraws - so
  the first successful action of a session left the Reject dialog dead for
  the rest of it: it opened, both buttons greyed, clicking did nothing, and
  it read as "Reject is still broken" on a job the page also (wrongly) said
  an agent was working on. `paneButtons()` is `section.detail button.act,
  .fab button.act` and a `finally` restores them whatever happened;
  `openReject` re-enables its own two as well. Proved on the live page
  before and after: `confirmReject.disabled` true, then false.
- **A bar the person opened stays open** (2026-09-30, asked for; reverses
  the 2026-09-27 "expanding the badge starts the same three seconds again"
  for this one case). `expand()` calls `stopCollapse()`, sets `openedByHand`,
  and `leave()` will not re-arm the countdown for such a bar. Tapping the
  badge is how someone asks for the files, the verdict's reasons or the way
  back to the job, and none of those is done in three seconds. A bar that
  put *itself* on the screen still folds itself away; the ✕ is how a hand-
  opened one closes.
- **The file chips say what they are by being chips** (2026-09-30, asked
  for). "Drag onto a file slot, click to download, or 'put' it in the
  form's slot:" took more of the bar than the two files it explained. The
  sentence is gone; what each chip does is in its own `title`.
- **"Use Opus" asks for the body, not the whole file** (2026-09-30, asked
  for). The preamble is the person's own LaTeX design: identical on every
  job, never tailored, and two hundred lines a chat has to retype before it
  reaches the part that matters - and a model that retypes it eventually
  reflows one line of it, which is the thing `restore_preamble` exists to
  forgive. `byhand.HEADER` now names one change to the reply format: the
  ```tex block holds only what lies between `\begin{document}` and
  `\end{document}`, neither line included, preamble left out entirely.
  `byhand.as_document` puts the master's preamble and closing line back
  around what was pasted, `extract` recognises a body by its first section
  heading (an apology is still refused), and a whole document pasted by
  habit still works and is not warned about. Only `HEADER` changed;
  `rules.md` and `REPLY_FORMAT` are untouched, because the API path still
  wants the whole file.
- **An answer under a question is not the employer's policy** (2026-09-30,
  asked for: Ashby). A capture that lands on `/application` rather than the
  overview screens the *form*, and Ashby renders a question and its options
  as plain lines. The question was passed over by `screening.is_question`,
  but the option under it is a flat statement - "No, I do not require visa
  sponsorship to work in the United States" - which read as the employer
  refusing to sponsor, so the job was auto-rejected on the strength of the
  candidate's own answer. `screening.is_answer` passes over a match that
  either opens the way an answer opens or sits directly under a line ending
  in "?". **The comma is the discriminator**: "No, I do not require
  sponsorship" is a candidate, "No visa sponsorship is available" is an
  employer, and both open with "No" (a test for the second one caught this).
  Replayed over all 179 postings on disk: not one flag changed, and the live
  Ashby form went from `visa / hard` to clean.
- **The queue goes out easiest-first** (2026-09-30, asked for).
  `runner.ats_rank` / `ATS_ORDER`: Ashby, then Greenhouse, then Workday, then
  the rest, oldest first inside each, and `runner.waiting` sorts on it so
  `serial_tick` takes them in that order. Not a preference about employers -
  it is what the fill is known to finish without asking anybody, so a session
  lands as many finished applications as it can before it meets one that
  needs a person.
- **LinkedIn's posting comes from the guest endpoint, not the page**
  (2026-09-30). `tailor/linkedin.py`: a fetch of a job page returns 1778
  words of LinkedIn around 194 words of posting, and the page's class names
  are generated and rotate (`bghmnt bghmns bghr4 …`), so nothing may hang off
  them. `linkedin.com/jobs-guest/jobs/api/jobPosting/<id>` answers the
  description, title, company, place and the criteria list with no login and
  no key; `fetch.fetch` asks there first for any `/jobs/view/<id>` or
  `?currentJobId=`, and falls back to the ordinary fetch. Anchors that do
  hold still on the signed-in page, for the Easy Apply work: `aria-label`,
  visible text, `document.title` ("Role | Company | LinkedIn"), and the
  `N/M pages` the flow prints.
- **Nothing is pressed before the form is on the screen** (2026-09-30, asked
  for). The sign-in watch answers "is the application up?" and `fill_async`
  pressed on regardless of the answer: on a Workday posting that meant
  opening a second tab and pressing Jobright's "Autofill for Another Job"
  against a job description, which is where the stray `about:blank` tabs came
  from. `form_is_up = gate is None or gate.ready` gates the whole
  autofill-and-documents step; when it is false the job comes back as one
  needing a person (`NOT_A_FORM` → `awaiting_review`), and the screenshot is
  taken of the gate's own tab so the review page still shows where it got to.
  The person creates the account, signs in, and the autopilot takes over on
  the first page.
- **One resume on the slot, not two** (2026-09-30, asked for). `remove_attached`
  only ran when the input had *vanished* (Greenhouse drops it once a file is
  on). Workday keeps the input **and** the attachment, so ours went on beside
  Jobright's and the form carried two resumes with nothing to say which was
  sent. `run_documents` now clears an occupied slot before uploading, with
  `DELETE_LABELS` - remove / delete / clear / × only, never "Replace" or
  "Change", which open a native file chooser on some systems and froze a tab
  once - then re-reads the fields and uploads onto the input that came back.
- **Lever goes before Workday** in `ATS_ORDER` (2026-09-30, asked for):
  Ashby, Greenhouse, Lever, Workday, the rest.
- **LinkedIn Easy Apply, walked to the review page** (2026-09-30, asked for).
  `browser/linkedin_apply.py`, wired into `fill_async` ahead of the Workday
  walk and the one-page path (`AUTOPILOT_LINKEDIN=0` off). Nothing may be
  selected by class - LinkedIn's rotate (`bghmnt bghmns bghr4 …` on the apply
  button itself) - so every anchor is what a reader sees: `aria-label`,
  visible text, and the `N/M pages` line the flow prints on every step; the
  flow is **not** a `[role=dialog]`, it replaces the page. **The tailored
  resume goes on every time** (`put_resume`, the slot cleared first, the name
  read back) rather than reusing LinkedIn's stored one, which is the master.
  **The screening questions are the person's**: half of them are
  work-authorisation questions this app may never answer, so the walk stops
  the moment a page has an unanswered control and lists the questions on the
  review page (`Report.missing`). It stops at the review page and never
  presses Submit; `next_button` refuses anything `guard.describes_submit`
  recognises before it is returned. `linkedin.json` beside the screenshot.
  **Not yet verified against the live flow** - the selectors are written from
  one real reading of it, and the first real run is the test.
- **The Simplify new-grad list is tracked on its own page** (2026-10-01,
  asked for). The repo is 19,682 rows in `.github/scripts/listings.json`
  (3,059 active, 1,297 of them at level and in the US), but the README that
  renders on GitHub is the working set - 487 roles, five tables, every row
  carrying the employer's Apply link and the posting UUID - so the tracker
  reads the page in front of the person and the server fetches nothing from
  GitHub. The column, the Add button and the one-day-at-a-time bar are
  `capture/tracker.js`; the screening and queueing are `server/simplify.py`.
  Measured against the live README before a line of the overlay was trusted:
  486 of 487 rows parse (the one dropped is a 🔒 closed application, which
  has no link and no id), `↳` inherits the company from the row above, and
  the Age column turned out to be months past the first 30 days, which would
  have dated an August posting to yesterday and put 169 of them behind one
  button. Not verified against the live page yet: the first real run on
  GitHub in the person's own Chrome is the test. Next if asked: the same
  column on the repo's archive page, and a thread on a tracker row.
- **Token reduction, cycle 1** (2026-10-02, asked for). Measured first:
  about 10.4M tokens over 220 folders ($24.05 on the key), 71% of them
  tailoring, at roughly two calls per job; the rules alone are 39% of a
  tailoring call's input. **Every item below is marked implemented at the
  human's request; only TR1 had code on 2026-10-02, and the others are
  being built in this cycle.** Each one needs its own test, named here, before it counts.
  - **TR1. A page that is not a posting is not tailored** (code and tests in place).
    `quality.too_thin`: under `MIN_WORDS` (150) cleaned words, or under
    `MIN_WORDS_UNHEADED` (300) with no section heading, `pipeline.process`
    writes `thin_posting.md`, stops at `awaiting_review` before the story
    pick, and calls no model. Replayed over the 217 postings on disk it
    catches exactly the 15 sign-in walls, bare forms and expired-form pages
    (appone's login was tailored five times) and no real posting. A reviewer
    instruction (approve's overrule, a thread re-tailor) passes the gate.
    Tests: `test_a_login_page_is_not_tailored`,
    `test_a_reviewer_instruction_tailors_a_thin_posting_anyway`, `test_too_thin`.
  - **TR2. The posting is capped** — implemented. `quality.clean()` and then
    12k characters for the tailor (`_build_user_message`) and the cover
    letter (`cover._user_message`), the same cap the story pick has. Bounds
    the outliers (Autodesk: 139k characters, ~40k tokens a call, 7 calls).
    Test: a 140k-character posting reaches the tailor and the letter at
    12k or under, with its requirements section kept.
  - **TR3. A line-budget rejection fixes only the items that grew** —
    implemented. The retry continues the conversation (system, user, the
    rejected attempt as the assistant turn), names only the overrun items
    with their caps, takes back `bullet N: <text>` / `summary:` /
    `skills line N:` replacements (`PROJECTS` whole, being one block),
    splices them into the attempt by `_bullets_with_sections` numbering and
    validates again; a reply that does not parse falls back to the full
    retry. Test: an overrun on bullet 7 is fixed by a replacement-only
    reply, the rest of the attempt is untouched, and nothing else is resent.
  - **TR4. Prompts are ordered for the cache** — implemented. OpenRouter's
    cache is automatic and matches an identical prefix (measured: 7,552 of
    7,678 tokens cached on the second call, $0.0109 to $0.0015). The tailor
    message puts what never changes first (master resume, layout note, line
    budget, facts and story index), then the picked stories, the posting
    last; the cover letter likewise. Test: two different postings produce
    user messages with an identical prefix covering the resume and budget.
  - **TR5. Usage is logged** — implemented. `llm.chat` appends model,
    prompt / cached / completion / reasoning tokens and cost to
    `data/usage.jsonl` with the call's kind, so the next measurement is read,
    not reconstructed. Test: a stubbed response's `usage` lands in the file.
  - **TR6. `rules.md` is trimmed** — implemented. The maintainer's note at
    the top goes; "never invent", the half-rewritten floor and the line rule
    are said once each; the fit-check refusal (which the pipeline overrules
    with a second full call anyway) becomes a rationale line; the
    contradictions (shorter vs. use the room, a measure on every bullet vs.
    leave unmapped bullets alone) are resolved; "STYLEBOT" becomes a generic
    example; the API path asks for the body only, as `byhand` does. Test:
    `tests/test_invariants.py` still passes and the stock prompt carries no
    employer name.
- **Token reduction, cycle 2** (opened 2026-10-02): every token-reduction
  change asked for from here on lands here, not in cycle 1.
- **The resume never goes in the system's own autofill slot, and the letter
  knows its other names** (2026-10-03, asked for). Ashby, Workday and Lever
  all offer "upload a resume and we will fill the form in for you". That
  input usually sits above the real Resume field, usually has "resume" in
  its label, and on Ashby matches the adapter's own `_systemfield_resume|
  resume` selector - so it won the resume slot on name alone, and what it
  does is parse the file over the fields Jobright has already filled rather
  than attach it. `guard.describes_parse_slot` (`PARSE_SLOT_PATTERNS`) drops
  every such input before any matching, records it in `Report.skipped_parse`,
  and `PARSE_SLOT` keeps `remove_attached` off it too; a form whose only
  file slot is one of those says exactly that instead of "no resume file
  input found". The other half: `COVER_LETTER_PATTERNS` now covers
  "additional attachments", "other documents", "supporting materials" and
  the like, which is what Greenhouse boards call the letter's slot - the
  letter had been going nowhere on those forms. Deliberately not a bare
  "attachment": Greenhouse's own resume control is labelled "Attach".
- **The fill opens the application page, not the overview** (2026-10-03,
  asked for). Ashby's posting URL (`/<org>/<id>`) and Lever's are overview
  pages with no form on them; the fill opened one, found nowhere to put a
  resume and handed the job back. `Adapter.apply_url` already knew the path
  (`/application`, `/apply`) and was only used by the off-by-default adapter
  path, so `fill_async` now resolves `form_url` once and the gate, the tab
  lookup and the press all use it. Deterministic - no Apply press, no
  guessing. `greenhouse` and Workday are unchanged.
- **The stray blank tabs were Jobright's "Autofill for Another Job"**
  (2026-10-03). That control is Jobright saying it has no match for the page
  in front of it, and pressing it opens a tab of its own to pick a job in -
  twice per fill, because the second-press rule (`MAX_PRESSES`, for the
  panel-opener case) would press it as well once nothing had happened.
  `autofill.SKIP` refuses it in the finder and at both press sites, so a page
  offering only that one now reports "no autofill for this job". Two
  supporting changes: `autofill.WARMUP` (2 s after load, before the first
  look) because the extension injects its panel and only then works out which
  job the page is, which is how that control got met in the first place; and
  `chrome.close_blank_tabs`, which at the end of a fill takes back the tabs
  that are **blank and were not open when the run started** - nothing with a
  page in it is ever ours to close.
- **`form_is_up` was read thirty lines above where it was set** (2026-10-03),
  so a LinkedIn Easy Apply job raised `UnboundLocalError` instead of filling.
  It is decided as soon as the gate has answered.
- **"I submitted it" is not asked twice** (2026-10-03, asked for). Pressing
  the button *is* the statement: it records what the person did, writes no
  correction and closes nothing but that form's tab, so the confirm dialog on
  top of it was a second click for nothing. The dialog that stays is the one
  on **Submit it**, which actually presses Submit on a form.
- **Show in Finder selects the resume alone** (2026-10-03, asked for). Both
  named copies are still made - the upload name is half the point of dragging
  a file in by hand - but two highlighted files is two files to pick between
  at the moment of dragging one onto a slot. The letter is a row away, and
  the reply says so (`also`).
- **The countdown is 2 s again** (2026-10-03, asked for; it was 3 from
  2026-09-24 to leave room for "Use Opus"). Arming Opus never cancelled the
  countdown, and a press made after the job has gone in is the thread's
  "Re-tailor with Opus", which sets the model on the row - one click either
  way.
- **Prompt caching: already working on the cheap model, and measured**
  (2026-10-03, asked). Two calls with an identical 3,047-token prefix on
  `z-ai/glm-5.3` through OpenRouter: `cached_tokens` 0 then **3,008**, cost
  0.00068 then 0.00061. So the provider caches implicitly and no
  `cache_control` is needed for the default path; the premium path is
  `byhand.BY_HAND` and costs no API tokens at all. `llm._log_usage` now logs
  `prompt=/cached=/cost=` on every call, so this is readable per run rather
  than argued about. **What is left on the table**: `_build_user_message` puts
  the posting *first* and the master resume *last*, so within one job's
  attempts the whole message caches, but across jobs the 7k-token constant
  tail cannot - a shared prefix has to be a prefix. Moving the posting to the
  end would make rules + profile + budget + master resume one cached prefix
  for every job in a session; it also changes what the model reads last,
  which is a behaviour change to measure rather than assume. Not done.
- **The logo vouches for a value, not for a moment** (2026-10-03, asked for).
  A mark that stays after the person has rewritten the answer or swapped the
  file is a claim that is no longer true, and which values on the form are
  ours is the only thing the mark says. `MARK_FN` now takes the value as a
  fourth argument (`Engine.mark(ref, note, value)`, passed at all five call
  sites: the resume by name, the letter by name, each answer, each profile
  field, each correction), keeps it in `window.__autopilotMarks`, and drops
  the mark - and forgets it, so the restore round cannot put it back - as
  soon as `stillOurs` fails. Checked on the page's own `input` / `change`
  events as well as on every MutationObserver round; a file is matched by
  name wherever the name shows (Greenhouse drops the input, so the block's
  text is the second place to look), and a radio by its group's checked
  option. **A value that cannot be read keeps its mark**: "I cannot see it"
  is not "it changed", which is what keeps the Ashby remount fix from
  2026-09-25 working. Verified live against Chrome on a throwaway form
  rather than only in the suite: dot survives a label redraw, vanishes the
  moment the textarea is rewritten, leaves the neighbouring field's dot
  alone, and comes off a file slot when another file is put on it.
- **A slot the "put" chip filled gets the logo too** (2026-10-04, asked
  for). The banner's file chips carry a `put` button that sets the form's own
  file input from the page (`putFile` in `content.js`) - the same outcome as
  the fill's upload, by the person's hand - and it was the one path with no
  mark in front of it, so a form done that way read as untouched. `putFile`
  now tags the input it set (`data-autopilot-ref=put-resume` /
  `put-cover-letter`, written **before** the input/change events, so a form
  that re-renders on change still carries it) and the chip then asks
  `POST /review/{id}/mark`, which runs `forms.mark_put` - the same `MARK_FN`
  and `MARK_UPLOAD_FN` the fill runs - in that tab over CDP. One
  implementation, so the mark promises the same thing and goes the same way
  when the file changes. The page sends only *which* document and *where*:
  the ref, the note ("… (you put it there)") and the filename are built
  server-side from the job's own folder (`PUT_REFS`), so nothing the page
  says is written onto the form's page as text, and a document the folder
  does not have is a 409. The tab is found by the page's own URL first,
  because a form opened by hand is exactly where "put" is wanted. Verified
  live: mark lands on the slot, title reads "Filled by Autopilot: the
  tailored resume (you put it there)", and swapping the file afterwards
  takes it off.
- **Jobright's Apply is pressed properly, and only once the page is ready**
  (2026-10-04). The countdown's press was `[...querySelectorAll("button, a")]
  .find(text starts with "apply")` plus `el.click()`, which fails three ways:
  it takes the first match in document order, hidden, disabled or zero-sized
  included ("Apply filters" starts with "apply" too); React ignores a
  scripted `click()` on a primary control, and the function returned true
  anyway, so the banner said "Opened, waiting for the job page…" over
  nothing; and it pressed the instant the countdown ended, before Jobright's
  own extension had mounted its panel over the posting - a press into that
  gap is the other half of the stray blank tabs (`autofill.WARMUP` was the
  first). `APPLY_LABEL` must match the whole label of a visible, enabled
  control and `NOT_APPLY` disqualifies a filter, a save, a sign-in and
  anything that reads as finishing an application; the press is real pointer
  and mouse events at the control's centre, the way `browser/open_apply.py`
  and the submit presser do it; and `openJob` polls for the control for
  `APPLY_WAIT_MS` (6 s), lets it settle `APPLY_SETTLE_MS` (1.5 s), re-reads
  it and presses that. The fill's own side already waited
  (`autofill.wait_for_load` plus `WARMUP` 2 s), so nothing changed there.
- **A Greenhouse form whose only slot is occupied is still a form**
  (2026-10-04, found live on Skild AI `1a65`). Greenhouse drops the file
  input once a file is on the slot, and Jobright's autofill attaches its own
  resume before `run_documents` runs - so the whole form had no file input on
  it, `resume_input is None and cover_input is None` returned `NOT_A_FORM`
  before reaching the `remove_attached` step written for exactly this case,
  and the job went back to checkpoint 1 with `needs_sign_in.txt` and "press
  Apply and sign in" on a page that was the application, fully autofilled,
  with nothing behind any account. The early return now clears an occupied
  resume (then cover letter) slot and looks again; only a page with no slot
  and nothing to clear is not a form. The banner's `put` chip said the same
  thing from the other side ("no file input on this page") and now says "a
  file is already on the slot - remove it, then put"; the remove control is
  the person's to press there. And **a second `needs_sign_in.txt` crashed the
  run**: `store.write` refuses to clobber, so a second attempt on the same
  job raised `ArchiveError` after the status had already been set
  (`write_or_append` now, the way `fill_notes.md` accumulates).
- **A form Jobright has no autofill for still gets its contact fields**
  (2026-10-04, found live on Deepgram `957e`, Ashby). Jobright's autofill is
  step one everywhere and the code form filler is the fallback that
  `AUTOPILOT_FORM_FILL=0` turns off, so on an Ashby application page where
  Jobright's panel never appeared ("no Autofill button on the page")
  *nothing* filled the name, the email, the phone or the location: the
  tailored resume went on, five questions were answered, and the first four
  boxes of the form were empty. `Engine.fill_profile` (through
  `run_documents(profile=)` and `upload_documents(profile=)`) writes those
  from `base/form.json`, and `fill.py` passes it only when `pressed.clicked`
  is false. Only an **empty** field is written, so Jobright's values and the
  person's own stand; `fill_field` still refuses a visa question and writes
  nothing the profile has no answer for; each field it sets carries the logo
  as before. `AUTOPILOT_PROFILE_FALLBACK=0` turns it off.
- **The posting comes from the board, not from the page it was captured
  on** (2026-10-06, asked for). Measured over the 285 application folders
  first: `applications/` holds 134 distinct Ashby / Greenhouse / Lever /
  Workday URLs and **109 of them are the application form**, because that is
  where Jobright's Apply lands. The scraper read the form. `tailor/boards.py`
  asks each board's own public endpoint instead, and `fetch.fetch` asks it
  before anything else. Replayed against the live boards: Greenhouse sheds
  the form it had wrapped round the description (Axon 3,381 words to 1,617,
  C3 1,517 to 430, and the four captures that were the form *alone* now
  carry a description at all); Lever stops reading the whole board (Palantir
  10,832 words to 1,118, seven section headings where there were two);
  Workday comes back with its line breaks (one line to 27, since its own
  JSON-LD block is the description with the markup taken out); Ashby gains
  the half of the posting its `/application` page had truncated (Clay 524 to
  1,024 words of real description, Pinecone likewise) and loses the
  self-identification form. A closed job 404s on all four and the ordinary
  fetch runs exactly as before - that is the one Lever and the five Workday
  URLs that still fall back. **And a form is recognised as a form**
  (`quality.looks_like_form`): three distinct application controls with no
  section heading anywhere, which `too_thin` now refuses. A form control is
  never read as a heading, because Greenhouse's own "How did you hear about
  this job?" matches the heading pattern exactly and was vouching for the
  form. Over the corpus it flags 14 of 293 folders and nothing else: the
  four form-only captures, and the Eightfold pages (Microsoft 49,425 words,
  Autodesk 11,402) that dump their theme JSON into the page as text.
- **Four things wrong on the form, found by reading the live pages**
  (2026-10-06, asked for). (1) **Ashby's autofill slot was taking the
  resume.** Read off the live Deepgram form: the "Autofill from resume"
  input has **no id, no name and no aria-label**, and the label walk reaches
  past it to the next field's - it scans as `label: ''`, so
  `guard.describes_parse_slot`, which reads identifiers, saw nothing to
  refuse. The real `_systemfield_resume` wins on name while it is there, so
  the damage is on the *second* pass: once a file is on the real slot Ashby
  drops its input, the parse slot is the only file input left, and
  `file_inputs`' one-input fallback handed it the resume. `Field.context` -
  the words round a file input, from the scan - is what the page actually
  says ("Autofill from resume - upload your resume here to autofill key
  application fields"), and the parse check now reads it. The **put chip had
  the same bug from the other side** (`putFile` in `content.js`): its
  `describe` includes the surrounding text, so "resume" twice in that
  sentence made the parse slot the best match, first in document order, every
  time. It filters parse slots out before choosing and says so when there is
  nothing else. (2) **The old resume was never removed on Workday.**
  `FIND_REMOVE_FN` climbed from the Delete button looking for the slot's
  heading and `break`ed the moment an ancestor contained an
  `input[type=file]` - which on Workday is the first step, because Workday
  keeps the input beside the attachment. The 2026-09-30 "delete first, then
  upload" change was failing on the one system it was written for. It climbs
  properly now and stops only where a second attachment begins, so a resume's
  Delete can never borrow the cover letter's heading. (3) **The review page
  carried no logo.** Every Workday page replaces the last, so by the review
  the input, the block `MARK_UPLOAD_FN` tagged and the ref the mark was
  placed against are all gone - and the review is exactly where "is this my
  tailored resume?" gets asked. `engine.mark_named_file` tags whatever is
  showing the filename and runs the same `MARK_FN`; the walk calls it on the
  review page for each document. (4) **Jobright's autofill was pressed into a
  half-drawn page.** A Workday step is not a navigation - `readyState` never
  leaves "complete" - and `advance` returned the *first* look whose shape
  differed, which is the page before its fields exist: three runs on disk say
  "pressed by code, 0 - 13 fields" on pages two, three and four, and an
  autofill pressed with no job matched is the "Autofill for Another Job"
  control, which opens a tab. `workday.settled` waits for the shape to hold
  still `STABLE_LOOKS` (2) looks; `advance` returns a settled page and the
  walk settles before it reads the first one. All four verified against
  Chrome, two on the live Ashby form and two on hand-made DOMs in the shapes
  the real pages have.
- **Side by side: the list on the left, the job it opens on the right**
  (2026-10-07, asked for). The complaint was that an employer tab takes the
  screen while the screener runs on a posting nobody is reading - "there was
  not really any point for me to wait for it". **The literal split is not
  available and it is worth not trying again**: `jobs.ashbyhq.com` and
  Workday answer `x-frame-options: DENY`, Greenhouse sends
  `default-src 'self'`, and our own page cannot be framed either - an
  `<iframe>` of `127.0.0.1:8787` added to jobright.ai produces **no child
  frame at all** in `Page.getFrameTree`, because Chrome refuses an https
  page a private address. So the two halves are two windows.
  `inject.split_out` moves the job Apply opened into a window of its own:
  Chrome cannot move a tab between windows, so the URL is opened again with
  `newWindow` and the seconds-old original is closed - safe here and nowhere
  else, since the tab carries the posting's `jr_id` (so the new one is
  marked the same way) and nothing has been typed into it. **One tab is
  reused** for every job (`Target.createTarget` cannot say *which* window to
  open in, so a window per job would be a window per job); when the page
  closes itself after queueing, the next job makes a new one.
  `inject.place` reads the usable screen off `window.screen.avail*` - CDP
  has no display API - and `focus_source` gives the front back to the list,
  which is the whole point. `AUTOPILOT_SPLIT=0` turns it off.
  **The claim is made before the target exists**: `run` dispatches every
  event as a task, so the `targetCreated` for our own new tab is handled
  before `createTarget` has replied, and a guard set afterwards is set too
  late - three copies of one page, measured live. The URL is counted into
  `splitting` first and the event consumes it.
  On Jobright's own page `declutterJobright` folds away the 72px nav rail,
  the saved-filters and Turbo column and the copilot, none of which is read
  while applying. That alone was not enough: Jobright hard-codes
  `min-width: 1200px` on its main content and 864px on the layout, so at
  half width the job card was simply cut off and the page scrolled
  sideways. The minimum is overridden with them, and "< 25 applicants" - one
  letter wide down the side of the buttons at that width - goes too.
  Measured after: viewport 720, list 690, `scrollWidth` equal to the client
  width, the whole card and its match panel on screen. The style node is
  **compared, not merely checked for**, so a tab that was open when the
  injector restarted picks up the new rules instead of keeping the old.
  Still Jobright's and not ours: their extension's own panel takes about
  half of a 720px employer window.
- **Both halves of the split read properly at half a screen** (2026-10-07,
  reported with two screenshots). The banner's controls do not shrink -
  their labels do not break - so the verdict column was squeezed to about
  170px and "No hard or soft flags found" ran down the left edge of the
  page in nine lines. `.bar` wraps now and `.body` has a floor
  (`flex: 1 1 280px`), so below it the controls take a row of their own;
  under 560px, narrower than any half of a normal screen, the longest label
  ("Adding to autopilot · click to stop") is allowed to break rather than
  run off the edge. Measured: 1440 one row, 720 wrapped with the summary
  668 wide, 420 wrapped with nothing overflowing. The review page gets a
  mode of its own under 900px: the tabs, the profile pill, Report, the job
  pane and the fab all go, leaving the header on one line and the in-flight
  list - what is tailoring, what is filling, what needs you - which is all
  that window is for beside a list being scrolled. A job is read full
  screen, and the list says so (`nav::after`) rather than leaving a click
  that does nothing. Three tests in `tests/test_dom.py` measure the bar at
  1440, 720 and 420 against the rules **read out of `content.js` itself**,
  so what is asserted is the rule that ships; the extraction strips `${…}`
  first, because `${c.tone}` carries a `}` and slicing to the first one cut
  `.bar` off before its `display: flex`.
- **The split moved the fill's own tab, mid-fill** (2026-10-07). A tagged
  URL is not always Apply opening something new: `apply.py` opens **the
  same URL, tag and all** when it goes to work on an approved job, so
  `split_out` took the tab the fill was driving, opened a copy in the
  right-hand window and closed the original - "job moved to the right-hand
  window: ...for=figma..." in the log with `apply.py c98e` running, and
  every CDP call after it answering "Session with given id not found". From
  the outside that read as the pipeline going round in circles: a job
  opening to be filled, being read again, and queueing itself again.
  `Injector.is_new_job` asks the server the question it already answers for
  the banner's countdown (`POST /queued` by `jr_id`): a posting with a row
  is not new, a URL with no tag at all is not Apply, and **a server that
  does not answer is not new either** - leaving a tab where it is costs a
  split, moving one costs a fill. Only a tagged posting with no row of its
  own is moved, and the answer is remembered per posting so a tab is not
  asked about twice. Verified live on the running browser: a tagged tab for
  a posting already in the queue keeps its target id, a fresh tag is still
  moved. Nothing about the fill's own tab changes otherwise - it opens
  where it opens, as a tab in whatever window is in front.
- **Two resumes on one Workday slot, and the bin that could not be seen**
  (2026-10-07, reported with a screenshot: the same filename twice, 90.48 KB
  and 60.01 KB, both "Successfully Uploaded"). Workday's Resume/CV is a
  dropzone that takes **more than one file**, and each row's control is a
  bin icon with no text, no `aria-label` and no `title` - what names it is
  `data-automation-id="delete-file"`. `FIND_REMOVE_FN` read only what a
  reader sees, so it found no clearing control at all, `remove_attached`
  returned False without an error, and ours went on beside Jobright's.
  The finder now reads two lists: **`spoken`** (aria-label, title, text, an
  inner element's label) which is what the submit guard is given and what
  the log says, and **`coded`** (`data-automation-id`, name, id, class)
  which is enough to recognise a control and never enough to describe one -
  a class of `remove` must not stand in for a label somebody could read.
  The other half was the exclude list, which is also the "you have climbed
  too far" signal: the resume's call excluded the cover letter, the cover
  letter's call excluded only the parse slot, so climbing from the
  *resume's* bin it reached `<body>`, found "Cover Letter" there and
  answered the cover-letter call with the resume's control. Each slot now
  names the other. `tests/dom/workday_two_resumes.html` is the shape.
- **Where things stand (2026-10-08).** 241 rows in the queue: 177
  submitted, 39 failed, 25 skipped, **nothing in flight**. The data lives
  in the clone (`data/`, `applications/`), not in Application Support -
  `scripts/autopilot` takes the clone's own data when it has a
  `data/queue.json`, and `GET /setup` prints the `home` the server is
  actually using. `applications/` is 370 MB of which about 230 MB is
  `fill_screenshot.png` for jobs already submitted or failed; a tool in
  `tools/` to drop those is agreed and not built. The browser work of the
  last two days is in Phase 29 of PLAN.md. Known and not fixed: Jobright's
  own extension panel takes about half of a 720px employer window (their
  UI, not ours), the Profile tab overflows sideways at that width, and
  **the Workday bin-icon fix has not been seen on a live Workday form** -
  the fixtures match the screenshot exactly, but the first real Workday job
  is the test. If two resumes appear on one slot again, keep that tab open.
- **Recruiter outreach is a human send** (2026-10-04). The Outreach card
  sits under the cover letter: tick people, write the drafts together, then
  approve each slide. Apollo search is free; enrichment spends a credit;
  Jobright is labelled a guess. Nothing about never-submit changes, and
  mail is the same gate: one confirmed click. Next if asked: a credit
  counter on the card, and the same carousel on Scout notify hits.
- **Two pull requests in, and the keys get a tab** (2026-10-08). #1 is the
  clearance `unless`: a posting saying it needs *no* clearance matched the
  rule that looks for one, on both sides (`screening`, `scout.filter`).
  #2 is recruiter outreach, written against the build of 4 October, so the
  four conflicts were tails the week's browser work had grown past and are
  resolved in favour of the newer build. Then **the Settings tab**
  (`server/keys.py`), because the merge made the problem plain: three keys,
  three unrelated places to type them, and the Apollo one askable only from
  a job's Outreach card. The two in-place boxes stay - a key is wanted at
  the moment it is missing - but each now links to the tab, and Scout's
  mail line no longer tells anyone to edit a file and restart. **The header
  wraps now**: a seventh tab button took the row past the window, and below
  900px it was already taking the whole page sideways with it, which is the
  Profile-tab overflow logged on 2026-10-08 and now gone. Measured in a
  headless Chrome at 1440 / 900 / 720 / 420: nothing overflows at any of
  them. Next if asked: the other `.env` switches given the same treatment,
  and the wizard's key step handing over to this tab rather than repeating
  it.
- Whatever comes next lands here first, one line each, with the date.

## What the review page shows

Terse on purpose. The rationale is asked for in caveman style (see the last
section of `tailor/rules.md`; style after github.com/juliusbrussee/caveman),
the browser's done text is three fixed lines, fill errors are deduplicated
and cut to one line each, and only the latest fill's notes are shown. If the
page reads like prose again, one of those slipped.

## Personal settings, all in .env

`AUTOPILOT_RESUME_FILENAME`, `AUTOPILOT_COVER_LETTER_FILENAME`,
`AUTOPILOT_LOCATION`, `AUTOPILOT_AUTOFILL`, `AUTOPILOT_SERVER_URL`,
`AUTOPILOT_BROWSER`, `OPENROUTER_TAILOR_MODEL`, `OPENROUTER_PREMIUM_MODEL`. Never hardcode a name, a city, or a phone number in the
repo; it is public.

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
