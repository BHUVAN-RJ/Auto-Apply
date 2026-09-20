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
- **Checkpoint 1 passes itself when `auto_fill` is on** (`data/settings.json`,
  default on, 2026-09-18). `pipeline.auto_approve`: clean tailor and a screen
  that is not `reject` → `approved` and `runner.start_fill`, no click. A
  poor fit or a `reject` screen still waits. The human's decision is
  checkpoint 2, the filled form; the fill never submits regardless.
  **Per job:** the banner has an `auto-approve` box next to "Add to
  autopilot", checked by default; unticked during the 2 s countdown the
  job waits at checkpoint 1 (`Job.auto_fill`, sent with `/capture`; on
  Jobright's posting page it goes ahead by `jr_id` through `POST /prefs`,
  in memory, used once, because the employer tab does the queueing).
  `None` on the row = the global switch. Nothing about it presses Submit.
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
- **A submission is marked two ways, both human.** The "I submitted it"
  button, and `POST /review/{id}/submitted-seen` from `capture/content.js`
  when the filled form's tab shows a confirmation (`CONFIRMED` regex, job
  id kept in `sessionStorage` across the navigation). The latter marks
  only a `filled` / `filling` job and never closes the browser. The agent
  reaches neither.
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
| `pipeline.py` | queued job → scrape → tailor → compile → cover letter → archive → checkpoint 1. Started by `/capture` |
| `apply.py` | approved job → named copies of resume/letter → fill form → screenshot → stop (checkpoint 2). Started by approve |
| `server/` | FastAPI on 8787: queue, review API, `runner.py` launches the scripts. `/capture` starts `pipeline.py` itself |
| `tailor/rules.md` | the tailoring prompt, sent verbatim — edit this, not the Python |
| `tailor/cover_rules.md` | the cover letter prompt, same rule |
| `tailor/cover.py` | letter from the tailored resume; plain pdflatex template; failure is non-fatal |
| `tailor/answers.py` + `answer_rules.md` | free-form form questions, answered by the tailor model (`OPENROUTER_ANSWER_MODEL` overrides) via the agent's `answer_question` action, and **by the human from the review page** (2026-09-18): `POST /review/{id}/ask` builds the same `Context` the fill would (posting, tailored resume, letter, profile with the picked stories, facts), the "Ask for an answer" card shows the reply with Copy, and `detail.questions` (`answers.open_questions` over `form_state.json` / `form_fill.json`: empty, `?` or > 60 chars, never visa) are one-tap prompts. Everything lands in `answers.md`; the page shows the whole file. Nothing is typed into the form by this path |
| `tailor/screen.py` + `screen_rules.md` | on-page auto-reject screen: posting vs `base/applicant.md` Facts, fixed category enum, verdict recomputed from flags in code. Validator drops every soft location flag and explicitly-US hard location flags; it parses graduation and "by" cutoffs so an earlier graduation cannot become a timeline caution |
| `server/screen.py` | `POST /screen`, URL-keyed cache in `data/screens.json`; the pipeline reuses it. **A confirmation page never reaches the model** (2026-09-20): `confirmation_quote` (the `watch.CONFIRMED` phrase, in a short page or near the top of a long one) answers `verdict: submitted` in string work, and when `seen` says the page is a filled job of ours, `review.mark_seen` marks it right there. Employer URLs reuse a cached Jobright verdict; without one, low-quality ATS text is combined with the saved Jobright copy in one model call |
| `server/settings.py` | `use_profile` in `data/settings.json`; the page pins it on, the header only reports whether a story exists |
| `tailor/profile.py` | what the models are told about the applicant. `context(slugs)` = profile.md + applicant facts + `base/stories/index.md` + the picked stories' `tailor.md` when the switch is on, profile.md alone otherwise. `pick(posting)` chooses the slugs (one cheap call); the pipeline records them in `stories_used.txt` and the cover letter and answers reuse them. `screen_facts()` falls back to facts derived from the resume, cached in `data/derived_facts.md` |
| `tailor/interview.py` + `interview_rules.md` | the profile interviewer: state machine on disk under `base/stories/` (`_interview.json`, `<slug>/state.json`), one streamed turn per candidate message, header (`COVERED` / `DONE`, then `---`) parsed in code; the body is labelled by line (`ack:` and `ask:` spoken, `note:` text only; `_Parts` strips labels mid-stream and tags each delta `spoken`), and the page speaks only tagged parts, falling back to first sentence plus questions when a reply has no labels. Writes `main.md` on close; `story_rules.md` is the prompt for `tailor.md` and `star.md`, written by the heavy model in a thread |
| `tailor/facts.py` | the facts interview, first thing after Start: eight fixed questions in code (authorisation, clearance, level, location, relocation, start date, graduation, form details), resume-derived facts and contacts offered as hints, each answer normalised to one literal line by the cheap model (`NORMALISE_PROMPT`, one follow-up max, "skip" = unknown), then `base/applicant.md` written. State in `base/stories/_facts.json`. `interview.skip_facts` / `restart_facts` (`/profile/facts/skip`, `/restart`); a redo mid-stories returns to `State.resume_phase`. The header pill shows Facts and Stories separately |
| `tailor/github.py` + `github_rules.md` | projects from GitHub (2026-09-20): `start_scan(handle)` lists the handle's public repos (forks and empty repos become `issues`), counts the handle's commits (one API call each; none attributed = counted whole and noted, the email is not linked), pulls each tarball off codeload (no git, no token, 40 MB cap), and the cheap model writes `base/stories/_github/<repo>/scaffold.md` plus 1-3 questions the repo cannot answer. `score`/`rank` in code (recency, stars, commits, README); the top `AUTO_PICK` and every repo matching a resume project are ticked. `confirm` → `interview.add_github_projects`: a match on a resume project folds the scaffold into that story ("Resume name (repo-name)", `start_fold` when it is already closed), the rest become experiences whose checklist is the questions (`Experience.questions`, lines `g1..gN`, closed in code after the last answer, no wrap-up); the interview reopens from `open` for them. `set_link` is the URL the resume hyperlinks (repo by default, store listing or site if the candidate says so): `Link:` line in `main.md` and `tailor.md`, rewritten in code. State in `base/stories/_github.json`. `AUTOPILOT_GITHUB_TOKEN` raises the rate limit; `OPENROUTER_GITHUB_MODEL` picks the reader |
| `server/github.py` | `/projects` status, `/scan`, `/{name}/scaffold`, `/{name}/pick`, `/{name}/link`, `/confirm`. The Projects tab (third header tab) polls it |
| `server/profile.py` | `/profile` status, `/profile/start`, `/profile/turn` (SSE, one JSON event per line), documents, regenerate |
| `voice/` + `server/voice.py` | speech: whisper.cpp in (`stt.clean` drops whisper's `[BLANK_AUDIO]`-style markers, standalone um/uh/erm/hmm and immediate word repeats); Fish Audio out when `AUTOPILOT_FISH_API_KEY` is set (`fish.py`, hosted, one request per sentence, `s2.1-pro-free` by default, voice pinned by `reference_id` because the API otherwise picks a new voice per request; delivery tuned by ear: `(cheerful)` tag, temperature 0.9, speed 1.08, all overridable in `.env`); Kokoro (ONNX, `kokoro.py`, needs brew `espeak-ng`) out, `say` when Kokoro is not ready, Piper via `AUTOPILOT_PIPER_BIN`. `assets.py` finds binaries and downloads models into the app data dir; `/voice/status`, `/setup`, `/transcribe`, `/speak` |
| `capture/content.js` | reads the page, calls `/screen`, paints the banner. Runs on Jobright posting pages (`/jobs/info/`) and on any tab opened from Jobright, either as the extension's content script or evaluated by `browser/inject.py` (then it talks to the server through the `__autopilotRequest` binding, not `fetch`). An `ok` or `caution` verdict counts down 2 s and acts by itself: off Jobright it queues the job, on Jobright's posting page it presses Jobright's Apply so the employer tab queues itself; a `reject` waits for the click. Successful Add carries the job id into `openReview(id, { focus: false })`: the Autopilot tab is pointed at the job (or created in the background) and **the active tab does not change** (2026-09-20); only "Open in autopilot" focuses it. The fill's own tab, on approve, is what comes to the front. After 5 s the bar collapses into a persistent, verdict-coloured assistant badge; expanding it reuses the same DOM/result and never screens again. Failed and `not_a_job` banners still offer Again and Add |
| `browser/inject.py` | the capture without an extension: attaches to our Chrome on 9333, evaluates `capture/content.js` in Jobright tabs and the tabs its Apply opens (by opener, or by the `?jr_id=` tag Jobright's extension puts on the URL), and answers the page's `__autopilotRequest` binding by making the HTTP call itself; the `/__open` path focuses/navigates an existing review-page tab or creates one over CDP (sites swallow `window.open`). `AUTOPILOT_SERVER_URL` changes the local origin and the injected script together when 8787 is occupied. Reconnects with backoff while 9333 answers, exits when Chrome is gone. Run by hand: `python -m browser.inject --open <url>` (lives in a Herdr pane). Never launches through browser-use, never closes the browser. **The one tab it closes** is the employer tab Apply opened, at that page's own `/__close` request once `/capture` has *created* the job (`Injector.closable`: a marked tab, never Jobright, never the review page; an "already in autopilot" page never asks, since a fill may be on it). The extension does the same with `chrome.tabs.remove` on `close-me` |
| `server/seen.py` | is this posting already in autopilot: `high` (same canonical URL, same `jr_id`, same ATS job id) or `confident` (same company and either title similarity ≥ 0.8 or posting simhash within 6 bits; rejections stop matching here after 90 days). `/screen` carries it as `seen`, the banner then never counts down and offers "Open in autopilot" (plus "Add anyway" at `confident`); `queue.add` dedupes by `same_job`; `/capture` refuses `jobright.ai` URLs outright |
| `tailor/quality.py` | posting text without a model: strips boilerplate lines, scores words + section headings + bullets; `pipeline.fetch_posting` picks the best of the fetched page, the browser's text, and Jobright's copy (fetched wins at ≥ 60% of the best) and records `**Text from:**` in `posting.md`; `simhash` for the seen check |
| `server/postings.py` | posting text the browser saw, for when the fetch gets a shell: `POST /posting` keeps Jobright's copy by posting id, `/capture` keeps the employer page's text; `pipeline.fetch_posting` falls back in that order. Metadata comes from `<role> @ <company> | Jobright.ai`, or the visible company / age / role header after "Original Job Post" when Jobright leaves `document.title` generic |
| `browser/autofill.py` | step one of the fill in code, no model: opens the form in a tab on our Chrome over raw CDP, finds the one control whose text starts with "Autofill" (page and every shadow root; Jobright's panel is a custom element), checks it against the submit deny-list, clicks it once, waits for Jobright's own word (`LISTEN_JS`: their extension posts `updateResultFromIframe` / `autoFillResultFromIframe` / `autoFillCompleteFromIframe` to `window.top` with `filledFields` / `missingFields` / `currentField`; read out of their bundle in `~/Library/Application Support/job-autopilot/chrome/Default/Extensions/odcnpip…/helper-app.*.js`), else its panel, read out of the `plasmo-csui#jobright-helper-plugin` shadow root (page `innerText` never sees it): `Autofilling` with three dots while it runs, then `N/M required fields filled` once it stops, so `panel.done` after `panel.busy` (or a count sitting untouched `SETTLE_POLLS`) is the deterministic finish; the filled-field count holding still is the last resort and never while the panel is busy. `from_status` reuses a tab on the panel count too, and never while it is busy. `missing` goes into the agent's task as "Jobright itself reported these empty". **Pressed by the fill, in its own fresh tab, on approve** (2026-09-18): the employer tab Apply opened is screened, queued and closed, so there is nothing to reuse. `inject.maybe_autofill` (press on load, `AUTOPILOT_AUTOFILL_ON_OPEN=1` brings it back) is off by default; when it is on, the fill `reuse`s that tab (`find_tab` by URL, Jobright's messages read back off `window.__autopilotJR`) instead of opening a second one, and only presses itself when no such tab exists. Jobright's panel has two steps on some pages ("Autofill my application" opens it, "Autofill" inside starts the fill), so a differently worded control gets one more press while nothing has happened (`MAX_PRESSES`). `Page.setInterceptFileChooserDialog` is on for the duration of the press: a native chooser froze a tab and every script in it. `signal_js` / `notify` tell the page's banner what automation is doing (`window.__autopilotAutomation(state, note)`): `working` = purple pulsing core + note line, `done`, `error`. The agent then `switch`es to that tab and its task says step one is done. Any failure = `clicked=False` and the agent does it as before. `AUTOPILOT_AUTOFILL_BY_CODE=0` turns it off |
| `browser/forms/` | the fill without a model, for Ashby, Greenhouse and Lever (`adapter_for(url)`, one module per system, `ADAPTERS` in `__init__`). `engine.py` scans every control over raw CDP (label resolved like a screen reader, `data-autopilot-ref` tags), matches by the adapter's id/name selectors, then `base/form.json` `answers` by exact label, then generic label patterns (never on a textarea or a label over 60 chars: those are questions), sets text through the native setter with real typing as fallback, `<select>` by option text, radios and Ashby's `button[aria-pressed]` by click, comboboxes by click + keys + clicking the suggestion, files by `DOM.setFileInputFiles` read back off the input or its block. Visa fields are skipped before matching whatever the profile says; no option or radio is clicked without `guard.describes_submit`. The report (`form_fill.json` next to the screenshot) is the agent's task: "these are still empty". `base/form.json` is gitignored, template `base/form.example.json`; missing = Jobright path as before. `AUTOPILOT_FORM_FILL=0` turns it off, and **it is off in `.env`** (2026-09-18): Jobright's autofill is always step one, this stays as the fallback |
| `server/corrections.py` | the correction loop. `browser/fill.py` writes `form_fill.json` with `after_agent` (the form as the agent left it, `forms.snapshot` over CDP, visa questions dropped) and `after_agent_meta` (the control's id / name / kind per label); the filled tab's `content.js` pings `POST /review/{id}/form-state` every 5 s, on submit clicks and on `pagehide`, and the server looks at the form itself (`capture`: same tab by id or URL, only while it is still on the job's host, ≥ 3 fields) into `form_state.json`; `/submitted` takes one more look then `learn` diffs, `/submitted-seen` only `learn`s. Changed or newly filled values land in `base/form.json` `corrections` by exact label as records (`value`, `was` = what the form held, `system`, `field`, `when`, `job`; `forms.profile.add_corrections`, the old `answers` key folded in on read and dropped on write). **Applied on the next fill** (2026-09-20): `Engine.apply_corrections` in `run_documents`, after the documents and before the questions, puts each corrected value over whatever Jobright left on any field whose label matches, never a textarea, never a visa question, logo note "corrected: … (autofill had …)"; `Report.corrected`, the notes say `corrected: …`; `AUTOPILOT_CORRECTIONS=0` turns it off. The Profile tab's Form details shows the table (field, form filled, you corrected, where) with delete. Never blocks marking submitted |
| `server/watch.py` | the server's own eyes on every filled form (2026-09-20), a thread started at app startup (`AUTOPILOT_WATCH=0` off; `TestClient(app)` without `with` never starts it). Every 4 s, for each `filled` / `filling` job with a `form_fill.json`: attach to its tab (`find_target` by the recorded id), `forms.snapshot(detail=True)` (fields, identifiers, visible text, URL). Form still there and on the job's host → `form_state.json`, the file the page's ping writes, so the correction loop has its final state without a single ping. No form and a `CONFIRMED` phrase → `review.mark_seen` (shared with `/submitted-seen`). The phrase alone is never enough: postings say "thank you for your interest"; the form has to be gone (`corrections.MIN_FIELDS`). The page's script does the same from inside the tab; this is for the times it is not there (tab never marked, navigation missed, `sessionStorage` blocked), which was "sometimes it works". Nothing pressed, nothing closed |
| `server/form.py` | the preliminary interview: `GET/POST /profile/form`, fixed `QUESTIONS` (contact, location, work, education, source pinned to Other, EEO, work authorisation), `/profile/form/hints` = contacts from the resume cached in `data/contacts.json`. The Profile tab's `Form` module walks them one per screen (Enter next, Skip, Save and stop), then shows the file and the learned answers with delete. Authorisation keys are on file but not in `forms.profile.KEYS`: collected, never auto-filled |
| `browser/ats.py` + `ats_rules.md` | per-system notes for the browser model, picked by URL (oracle, greenhouse, ashby, workday, lever), sent verbatim under "Notes for this application system". Edit the markdown, not the Python. Oracle: one "Upload Attachment" control for every document, so the upload guard lets the cover letter onto a generic attachment slot (never onto a resume-named one) |
| `browser/guard.py` | the never-submit deny-list |
| `browser/chrome.py` | launches and reuses the Chrome that browser-use attaches to |
| `tex/compile.py` | engine picked per document, not fixed |
| `archive/store.py` | immutable per-application folders |
| `review/index.html` | the whole UI, one file, no build step. **Opens on the first job in flight** (`loadList`: `inflight[0]`, list order needs-you first, then the agent's), never one from a closed shelf; nothing in flight = "Nothing in flight." in the pane. `/#<id>` (the banner's "Open in autopilot") is read at boot and on `hashchange`; it was ignored until 2026-09-19. Terminal look (mono, square, purple = agent, green = you, red = rejected); `BUCKET` / `VERB` / `ORDER` at the top drive the in-flight list and the two closed shelves; the floating `.fab` is the decision; nothing internal (models, pids, folders, commands) is shown. Holds the profile state pill in the header, the Profile tab (chat, seed files, documents) and the floating voice orb, always on: opening the chat speaks the open question and then listens, a tap pauses (red, "Paused", text only both ways), the orb drags anywhere and remembers its spot, leaving the chat stops everything (`Profile`, `Bubble` and `Voice` modules at the bottom; the orb is a flat SVG ring in the page's ink, page-colour fill, hard offset shadow: a fixed circle plus three standing-wave modes on springs, kicked by the audio level, so it bounces but never changes shape; one flat core inside grows with the level, green only while listening, purple while speaking; thinking is an arc sweeping the rim with a bulge under it (peak at the head, fading to the tail, `ARC`/`BULGE`/`SOFT` in `Bubble`); paused is a red dashed ring with the word, error a red ring; only `ack:`/`ask:` parts of a reply are spoken, with a pause between; silence cut-off constants `SPEECH`, `SILENCE_MS`; barge-in is behind `BARGE_IN = false`) |
| `capture/background.js` | context menu, follows tabs off Jobright to inject the screen wherever Apply lands, and focuses/reuses the unpacked extension's existing Autopilot tab after capture |
| `tools/sweep_failed.py` | moves `failed` application folders under `applications/failed/` and repoints queue rows; nothing deleted |

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
- **Something else can hold 8787 too.** Caveman Cloud's `caveman-proxy`
  (`~/.caveman/bin`, started by `caveman codex`, detached under launchd,
  no plist) listens on 127.0.0.1:8787 and answers everything with
  `cave_route_not_found`. `lsof -nP -iTCP:8787 -sTCP:LISTEN` names the
  holder. `AUTOPILOT_SERVER_URL` moves the injector and its injected
  script together when the server has to live elsewhere.
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
- **Rejecting a `filling` job does not stop its fill.** `apply.py` keeps
  running on a rejected job and, with no form to work on, wandered into
  another job's tab. `pgrep -fl apply.py` and kill it by hand for now.
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
- **A form question is not a requirement.** The screen's first real run
  flagged "Will you now or in the future require sponsorship?" as a visa
  flag. The rules say bare questions are not flags; if it recurs, that is
  the section of `screen_rules.md` to sharpen.
- **A stretch is not a reject.** The screen rejected every other US city,
  cohorts a year off, and 1.5 years against "0-1". `screen_rules.md` lists
  hard / soft / never per category; add a new false positive to the
  "never" line of its category, not to the prose.
- **Two screen rules are also code invariants now.** Every US location is
  green, not caution; a degree earned before an "earned or expected by"
  deadline satisfies it. `enforce_location_policy` and
  `enforce_timeline_policy` correct model output before `verdict_for` runs.
  Update their regression tests as well as `screen_rules.md` when changing
  either policy. Cached US location flags are normalized on read; the known
  timeline cache entry was repaired when the date validator shipped.
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
- **The preamble check is byte-exact** (`_check_frozen_sections`; only
  whitespace runs are collapsed). The model burned two of four attempts on a
  commented-out font line and a dropped space in a macro. `rules.md` now says
  nothing above `\begin{document}` changes, not even a comment.

## Testing

`pytest` collects 512 tests. The suite stubs the model, browser, lualatex, and
speech binaries, so **passing tests do not mean it works** — every real bug so
far survived a green suite and appeared on the first real run. On the current
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
`AUTOPILOT_BROWSER`. Never hardcode a name, a city, or a phone number in the
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
