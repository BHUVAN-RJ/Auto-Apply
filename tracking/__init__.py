"""Watching the one browser we own, so that automating it is based on what
actually happens in it.

Three parts, and the split matters:

- `policy.py` — what may be tracked and what may never be. Pure, so the one
  decision with an edge to it (characters, not shapes) is readable and
  tested in one place instead of living in the drain.
- `recorder.js` + `collector.py` — the watching. Every tab in that Chrome,
  every click, key, edit, error raised and cleared, and every tab opening and
  closing with the reason attached.
- `shadow.py` — the parallel run. Our own selector table resolved against
  each real page, read-only, scored against what the live flow actually did.

It presses nothing, types nothing, closes nothing and navigates nothing. The
live flow — Jobright's autofill, `browser/`, the person's own hands — is
exactly as it was.
"""
