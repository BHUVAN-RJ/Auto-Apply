# Installing Autopilot

**If you are a person:** open Claude Code in a terminal and paste this:

> Install Autopilot from https://github.com/BHUVAN-RJ/Auto-Apply. Follow INSTALL.md in that repository.

Claude Code does the rest and tells you the few things only you can do:
paste your OpenRouter key into the app, sign in to Jobright, and answer
the profile interview. You need an Apple Silicon Mac, Google Chrome and
[Homebrew](https://brew.sh).

By installing it you accept that you use it at your own risk. Autopilot
never submits an application: you read every filled form and press Submit
yourself, and what you submit is your responsibility, not the authors'.
See the licence.

---

**If you are Claude Code**, the rest of this file is for you. Work
through it in order. Tell the person what each step is about to do
before you do it, and stop and ask whenever a step says so.

## 0. What you are installing

A local app. A Python server (FastAPI, port 8787 or the next free one)
holds a job queue, tailors a LaTeX resume and a cover letter to each
posting through OpenRouter, and fills application forms in a Chrome it
owns, **stopping before Submit, every time**. Read CLAUDE.md in the clone
before you change any code: its first section is the rule the project
exists to protect.

Two places on disk, never mixed:

- **the clone** (code only, updated with git): `~/Autopilot` unless the
  person wants it elsewhere;
- **the data folder** (`~/Library/Application Support/Autopilot`): the
  master resume, the profile, the stories, the applications, the edited
  prompts, the OpenRouter key. Never copy any of it into the clone, and
  never commit or push it.

## 1. Check the Mac

- `uname -m` must be `arm64`. Intel Macs are not supported; say so and stop.
- Homebrew: `command -v brew`. If it is missing, show the person the one
  command on https://brew.sh and ask them to run it themselves (it needs
  their password), then continue.
- Google Chrome in `/Applications`. If missing, ask them to install it.

## 2. Clone and pick the release

```bash
git clone https://github.com/BHUVAN-RJ/Auto-Apply ~/Autopilot
cd ~/Autopilot
LATEST=$(git tag --list 'v*' --sort=-v:refname | head -1)
git switch -c mine "${LATEST:-origin/main}"
```

The branch `mine` is the person's copy. Every change you ever make for
them is a commit on it, and updates are merged into it (UPDATING.md).

## 3. Run the installer

Tell the person it installs four Homebrew packages (uv, tectonic,
espeak-ng, whisper-cpp) and a Python environment inside the clone, then:

```bash
scripts/install.sh
```

It is safe to run again. It stops with a plain sentence if something is
missing; fix that and rerun.

## 4. The master resume

The tailor reads exactly one layout: `base/resume.template.tex` (also
copied into the data folder). Its section headings
(`\section{\texorpdfstring{\color{airforceblue}SUMMARY}{}}` and the same
for EDUCATION, EXPERIENCE, PROJECTS, TECHNICAL SKILLS) and its
`\resumeItem{...}` bullets are what the checker counts and measures. A
resume in any other layout, even valid LaTeX, cannot be tailored: every
attempt is rejected. So whatever the person hands you, the master resume
is **their content in this template**.

Ask the person for their resume (PDF, Word or `.tex`), then:

1. Copy `base/resume.template.tex` to
   `~/Library/Application Support/Autopilot/base/resume.tex`.
2. Replace the made-up person (Alex Morgan) with theirs: header, summary,
   education, every role with its bullets as `\resumeItem`, projects
   (a link goes in the `\href` of the project's title), the skills as
   `\textbf{Category:}` lines, anything else under ACHIEVEMENTS or a new
   section of the same heading form. Keep every word, date, number and
   link from their file; invent nothing; drop nothing without asking.
   A section they do not have (no projects yet) stays, with one honest
   entry from their coursework or work, after asking them for it.
3. Keep it to one page (two if theirs was two). Compile it and check:

   ```bash
   AUTOPILOT_HOME="$HOME/Library/Application Support/Autopilot" .venv/bin/python -c "
   import paths; from tex import compile as c; from tailor import tailor
   tex = (paths.BASE / 'resume.tex').read_text()
   print(tailor.master_problems(tex) or 'layout ok')
   out = c.compile_pdf(paths.BASE / 'resume.tex', paths.BASE / 'resume.pdf')
   print(out, c.page_count(out), 'page(s)')"
   open "$HOME/Library/Application Support/Autopilot/base/resume.pdf"
   ```

   `layout ok` is required. Ask them to compare the PDF with their
   original and fix what they point out; the tailor rewrites this file for
   every job, so it has to be right.

Rerun `scripts/install.sh` once the file is there: it compiles
`resume.pdf`, which the tailor measures line widths from.

Then set the names the uploaded files get, from the name on the resume,
in `.../Autopilot/.env`:

```
AUTOPILOT_RESUME_FILENAME=First_Last_Resume.pdf
AUTOPILOT_COVER_LETTER_FILENAME=First_Last_Cover_Letter.pdf
AUTOPILOT_LOCATION=City, ST
```

## 5. Start it

```bash
open ~/Applications/Autopilot.app
```

A Chrome window opens on the Autopilot page. This Chrome is the app's own
(its own profile, separate from their everyday Chrome); it stays open,
and that is intended.

## 6. What only the person can do

Tell them, in this order, and wait for each:

1. **The OpenRouter key.** In the yellow bar at the top of the Autopilot
   page. They make one at https://openrouter.ai/settings/keys and add a
   few dollars of credit there; the default model costs a few cents per
   job, the "Use Opus" button about twenty. **Never ask for the key in
   this conversation** and never write it into a file yourself: the page
   checks it with OpenRouter and stores it on their Mac only.
2. **Jobright.** In that same Chrome window: install the Jobright
   extension from the Chrome Web Store and sign in to jobright.ai. Jobs
   flow in from there: the banner on a posting screens it and, when it is
   clean, queues it by itself.
3. **The profile.** The Profile tab: press Start. A short facts interview,
   then one story per job and project on the resume, by voice or text
   (15-30 minutes, can be paused). Then the Form section (contact,
   education, the usual form questions). The better this is, the better
   every resume.
4. Optional: the Projects tab reads their public GitHub projects into the
   interview.

## 7. Check

```bash
scripts/autopilot status
curl -s "http://127.0.0.1:$(cat ~/Library/Application\ Support/Autopilot/run/port)/setup"
```

`key`, `resume` and `chrome` all `true` means it is ready. Logs are in
`~/Library/Application Support/Autopilot/logs/`.

## When something goes wrong

- **The page shows old behaviour:** `scripts/autopilot restart`. The
  server runs the code it started with.
- **Port 8787 is taken** by something else: the launcher already picks the
  next free port and tells the injector.
- **The first resume compile takes a minute or two:** Tectonic is
  downloading LaTeX packages once. Later compiles take seconds.
- **A form was not filled:** read the job's page first; the failure is
  written there in one line. CLAUDE.md, "Things that will bite you", has
  every failure seen so far.
- Anything else: the person can ask you to fix it. Follow UPDATING.md
  ("Changing the code") so the fix survives the next update.
