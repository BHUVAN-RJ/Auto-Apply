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
  call `browser.kill()`; `stop()` ends the session without closing Chrome. A
  test asserts `kill()` is absent from the module.
- A poor-fit verdict is a *recommendation*. It stops at checkpoint 1 for the
  human; it does not set `skipped`.
- `filled` requires the agent to have actually finished. A screenshot only
  proves the browser was alive.

## Layout

| Path | What |
|---|---|
| `pipeline.py` | queued job → scrape → tailor → compile → archive → checkpoint 1 |
| `apply.py` | approved job → fill form → screenshot → stop (checkpoint 2) |
| `server/` | FastAPI on 8787: queue, review API, `runner.py` launches the scripts |
| `tailor/rules.md` | the tailoring prompt, sent verbatim — edit this, not the Python |
| `browser/guard.py` | the never-submit deny-list |
| `tex/compile.py` | engine picked per document, not fixed |
| `archive/store.py` | immutable per-application folders |
| `review/index.html` | the whole UI, one file, no build step |

Config lives in `.env` (gitignored). `base/resume.tex` is the master resume and
is **gitignored** — the repo is public and the resume carries real contact
details. Same for `base/profile.md` and `base/applicant.md`.

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
- **Chrome will not share a profile directory** with a running instance — it
  silently falls back to a throwaway and the login is lost. The fill loop owns
  `~/Library/Application Support/job-autopilot/chrome`.
- **A stale uvicorn holds port 8787** and serves old code. If behaviour makes
  no sense, `pgrep -fl uvicorn` first.

## Testing

`pytest`, 165 tests, no network and no browser. The suite stubs the model, the
browser, and lualatex, so **passing tests do not mean it works** — every real
bug so far survived a green suite and appeared on the first real run. Run
something end to end before claiming a fix.

## Style

Full English in the repo: commits, comments, docs, this file. Conversation in
the terminal follows whatever mode is active.

Commits explain *why*, not what changed. The diff already says what changed.
