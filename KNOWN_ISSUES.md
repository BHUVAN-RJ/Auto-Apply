# Known issues

Defects that are **understood, reproducible and not fixed**, each because
the fix is a change of shape rather than a line. This file is for the ones
worth fixing; a decision deliberately taken is in CLAUDE.md instead, and
something nobody has reproduced belongs in a GitHub issue, not here.

Each entry says what goes wrong, how to see it, what is actually causing it
(or that this is unknown), and what a fix has to deal with. An entry leaves
this file when it is fixed, with the date, in CLAUDE.md.

---

## 1. Job ids collide, and a collision hides a job

**Raised** 2026-10-09, while deleting a queued row the page offered no way
to delete. **Severity: high** — it loses access to a job and can act on the
wrong one.

`Job.id` is `url_hash`, **four** hex characters of sha256: 65,536 ids for
270 rows. Two Twitch postings (an IBM-hosted one and a Greenhouse embed)
both hashed to `96f2`.

`queue.get` returns the first match, so for the newer of two colliding jobs:

- no detail page — `/#<id>` opens the older job
- no Reject, no Try again, no way out of the queue
- `/review/{id}/...` can act on the **other** job: a reject, a fill, a
  submitted mark
- `pipeline.folder_name` can clash for two jobs captured the same day

At 270 rows the chance of at least one collision is about **43%**, so this
was due rather than unlucky, and it grows with every job added.

**To see it**

```bash
python3 -c "
import json; from collections import Counter
rows = json.load(open('data/queue.json'))
rows = rows if isinstance(rows, list) else rows.get('jobs', rows)
ids = Counter(r.get('url_hash') for r in rows)
print([(i, n) for i, n in ids.items() if n > 1])
"
```

**Why it is not a one-liner.** Lengthening the hash changes every id on
file: 270 queue rows, the application folder names, `data/threads/<id>.json`,
`data/apply_<id>.log`, the `#<id>` links the banner writes into pages, and
every status note that names one. A fix is a migration: keep the short id as
a **display** name, key on something longer, and map the old ids forward so
nothing already on disk is orphaned.

**Where it stands (2026-10-14).** The long key now exists and nothing has
been migrated onto it. `identity.identity_key(url)` is 64 bits of sha256
over the *canonical* URL, so one job keeps one key across the several links
it arrives as, and `identity/` keys its own index on it while showing the
four-hex id. The check is no longer a hand-written script:

```bash
python -m identity collisions
```

which today answers that **no four-hex id in the data names two jobs** - the
`96f2` pair was resolved by hand. The residue of that pair is still on disk
and is worth knowing about: the surviving queue row carries the **IBM** URL
with the title "...at Twitch" and an `app_dir` pointing at the Twitch
folder, whose `posting.md` is the Twitch posting under a `**Source:**` line
naming careers.ibm.com. That row's URL and its contents disagree
permanently; no fix recovers which was meant.

The migration proposed, and not done, in the order that matters: (1) add
`key` to `Job`, defaulted to `identity.identity_key(url)`, leaving `Job.id`
exactly as it is, and give `queue` a `by_key` lookup; (2) make
`queue.get(id)` refuse an ambiguous short id rather than silently return
the first match - **that is the data-loss bug, and it is the whole fix**;
(3) route `/review/{id}/...` through the long key where the page can supply
one; (4) leave folder names, thread files, logs and `#<id>` links alone for
good, because a display name may repeat as long as nothing acts on it.

---

## 2. A LinkedIn posting is never queued

**Raised** 2026-10-11 ("the LinkedIn ones open and nothing happens"), scope
measured 2026-10-14. **Severity: medium** — no data is lost and nothing is
damaged, but one whole source silently does not work.

Jobright's Apply on a LinkedIn job opens the tab and the injector attaches
to it. From there, nothing: **47 distinct LinkedIn job pages have been
injected over the life of the install and not one has ever produced a queue
row.** Only 6 of those 47 were ever screened at all, and those 6 came back
`ok` (5) and `caution` (1) — verdicts that queue themselves anywhere else.

So there are two failures stacked, and the second is the interesting one:

1. most LinkedIn tabs are never screened
2. a LinkedIn tab that **is** screened `ok` still does not queue

**To see it**

```bash
# tabs the injector attached to
grep -oE "linkedin.com/jobs/view/[0-9]+" logs/inject.log | sort -u | wc -l

# verdicts cached for those pages
python3 -c "
import json; from collections import Counter
d = json.load(open('data/screens.json')); items = d.get('screens', d)
rows = items.items() if isinstance(items, dict) else [(r.get('url'), r) for r in items]
li = [v for u, v in rows if 'linkedin.com/jobs/view' in (u or '')]
print(len(li), Counter(v.get('verdict') for v in li))
"

# queue rows for a LinkedIn posting: none
python3 -c "
import json
rows = json.load(open('data/queue.json'))
rows = rows if isinstance(rows, list) else rows.get('jobs', rows)
print([r.get('url_hash') for r in rows if 'linkedin.com/jobs/view' in (r.get('url') or '')])
"
```

**Cause: not established.** What is known, and what each of it rules out:

- `tailor/linkedin.py` reads the posting from the guest endpoint, so a
  LinkedIn URL is not short of text — the screen is not starving.
- `/capture` refuses `jobright.ai` only; LinkedIn is not on a deny-list.
- The countdown that queues a job is suppressed for a tab marked
  `__autopilotHandOpened` or `__autopilotCold`, and for a `seen` match. A
  LinkedIn tab opened by Jobright's Apply should be none of those, and
  that assumption is the first thing to check.
- `submissionWatched` is restored from `sessionStorage` per tab and silences
  the banner; `notTheConfirmation()` was supposed to bound that
  (2026-09-26) and LinkedIn is a single-page app that rewrites the URL
  without a navigation, which is exactly the shape that fix did not cover.

**What a fix has to do first.** Reproduce with the tab kept open and read
the page's own state (`__autopilotHandOpened`, `__autopilotCold`,
`submissionWatched`, whether `schedule()` ran) rather than reasoning from
the logs, because the logs show the injection succeeding and say nothing
about what the banner decided afterwards. Separately: `browser/linkedin_apply.py`
(the Easy Apply walk) has never run against the live flow either, so these
two are likely to be looked at together.
