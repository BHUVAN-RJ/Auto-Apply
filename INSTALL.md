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

**The master resume is the person's own LaTeX source, never a conversion.**
Do not write one from a PDF or a Word file: the tailor edits the source,
and a resume rebuilt from a PDF loses and guesses things the person then
signs. If they have no LaTeX resume, they make one themselves from the
template (below) and come back; do not do it for them.

The tailor reads exactly one layout: `base/resume.template.tex`. Its
section headings (`\section{\texorpdfstring{\color{airforceblue}SUMMARY}{}}`
and the same for EDUCATION, EXPERIENCE, PROJECTS, TECHNICAL SKILLS) and its
`\resumeItem{...}` bullets are what the checker counts and measures; any
other layout, even valid LaTeX, cannot be tailored.

The person can upload the `.tex` on the app's setup screens (step 6); it
is compiled and checked there. Or they give you the file, and:

1. Copy it (and any `.cls` / `.sty` / font it needs) to
   `~/Library/Application Support/Autopilot/base/`, the main file named
   `resume.tex`.
2. Check it:

   ```bash
   AUTOPILOT_HOME="$HOME/Library/Application Support/Autopilot" .venv/bin/python -c "
   import paths; from tex import compile as c; from tailor import tailor
   tex = (paths.BASE / 'resume.tex').read_text()
   print(tailor.master_problems(tex) or 'layout ok')
   out = c.compile_pdf(paths.BASE / 'resume.tex', paths.BASE / 'resume.pdf')
   print(out, c.page_count(out), 'page(s)')"
   ```

3. `layout ok`: done. Otherwise their LaTeX is in another layout: with
   their go-ahead, move its content into `base/resume.template.tex`
   **LaTeX to LaTeX, word for word**: every heading, entry, date, number
   and link from their file, each bullet as a `\resumeItem`, skills as
   `\textbf{Category:}` lines, anything else under ACHIEVEMENTS. Invent
   nothing, drop nothing without asking. Save the result as `resume.tex`
   (their original stays beside it as `resume.original.tex`), check again,
   `open` the PDF, and have them compare it with their own.

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

The app opens on its onboarding: a few screens in the Autopilot window
that walk the person through what only they can do. Tell them it is
there and let them go through it:

1. **The OpenRouter key.** They make one at
   https://openrouter.ai/settings/keys (the screen opens it), add a few
   dollars of credit, and paste it. The default model costs a few cents
   per job, "Use Opus" about twenty. **Never ask for the key in this
   conversation** and never write it into a file yourself: the screen
   checks it with OpenRouter and stores it on their Mac only.
2. **The resume**, as their `.tex` file, if it is not in place from step
   4 already: uploaded, compiled and checked on the screen. A layout the
   tailor cannot read is said there; the fix is step 4, point 3.
3. **Jobright.** The screen opens the Chrome Web Store page for Jobright's
   extension and jobright.ai, as tabs in the Autopilot Chrome (the one the
   forms are filled in; their everyday Chrome does not count). It notices
   the extension by itself; they tick "I'm signed in" once signed in.
4. **The profile interview**, offered on the last screen, is optional. It
   makes the tailoring better (the tailor uses what they did in each role
   in their own words); they can start it then or any time from the
   Profile tab.

If they skip a step, the app shows what is still missing in a bar at the
top until it is done.

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
