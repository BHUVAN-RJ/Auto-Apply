"""The correction loop: what the human changed on the form is what the
fill got wrong, and next time that field is filled from the correction.

Two snapshots of the form, both question -> value, both taken by
`browser/forms/engine.snapshot` over CDP so the labels match what the
filler uses:

- `after_agent`, in `form_fill.json`, written by `browser/fill.py` when
  the agent stops. The baseline.
- `form_state.json`, the latest look at the form while the human works
  on it. The tab's capture script pings `/review/{id}/form-state` every
  few seconds and on its way out; the "I submitted it" button takes one
  more before the browser closes.

`changes` diffs the two: every value that is not empty and not what the
fill left. The human picks which of those to keep, on the form's own
banner or on the review page, and `remember` writes the picked ones
into `base/form.json` under `corrections`, keyed by the question as the
form showed it, with what the form held before (Jobright's value,
usually), the system, the control's id and name, and the date. Nothing
is recorded that was not picked: a diff at submit time used to be
learned by itself (2026-09-20) and that is the AI deciding what the
human meant. The next fill on any of the three systems puts the
corrected value over Jobright's by exact label
(`Engine.apply_corrections`); the Profile tab shows the table. Visa and
work-authorisation questions are never in either snapshot
(`snapshot_of` drops them), so nothing about them is ever offered.
Every failure here is a note, never an error: marking a job submitted
must not depend on a browser being reachable.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Optional

from browser import ats, chrome, forms, guard
from browser.forms import profile as form_profile

FILL_REPORT = "form_fill.json"
FORM_STATE = "form_state.json"
NOTES = "corrections.md"
MIN_FIELDS = 3   # fewer is a confirmation page with a search box, not a form


def cdp_url() -> str:
    return os.environ.get("AUTOPILOT_CDP_URL", "").strip() or chrome.cdp_url(chrome.port())


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def capture(job_url: str, app_dir: Path) -> dict:
    """Look at the form now and keep it as the latest state. Finds the tab
    by the id the fill recorded, else by the job's URL. Nothing is touched
    on the page."""
    saved = _read(app_dir / FILL_REPORT)
    url = cdp_url()
    target = forms.find_target(url, job_url, str(saved.get("target_id") or ""))
    if not target:
        return {"captured": False, "reason": "form tab not open"}
    # A ping sent on submit can arrive after the tab has moved to the
    # confirmation page: a look at another site, or at a page with a
    # search box and nothing else, must not replace the last good state.
    if not forms.same_site(url, target, job_url):
        return {"captured": False, "reason": "tab has left the form"}
    try:
        look = asyncio.run(forms.snapshot(url, target, detail=True))
    except Exception as error:  # noqa: BLE001
        return {"captured": False, "reason": str(error)[:200]}
    fields, meta = look.get("fields") or {}, look.get("meta") or {}
    if len(fields) < MIN_FIELDS:
        return {"captured": False, "reason": "no form on the page"}
    try:
        (app_dir / FORM_STATE).write_text(json.dumps({"fields": fields, "meta": meta, "target_id": target}, indent=1))
    except OSError as error:
        return {"captured": False, "reason": str(error)}
    return {"captured": True, "fields": len(fields)}


def diff(baseline: dict, final: dict) -> dict:
    """What the human changed: filled in, or replaced. Clearing a field
    teaches nothing; a visa question is refused even if it slipped in."""
    out: dict = {}
    for label, value in final.items():
        value = str(value or "").strip()
        if not value or value == str(baseline.get(label, "") or "").strip():
            continue
        if guard.describes_protected(label):
            continue
        out[label] = value
    return out


def changes(app_dir: Path, store: Optional[dict] = None) -> list[dict]:
    """What the human changed on the form since the fill left it: label,
    what autofill had, what it is now, and whether that exact correction
    is already on file. Read off the two snapshots; nothing is decided
    here. Visa questions never appear."""
    report = _read(app_dir / FILL_REPORT)
    baseline = report.get("after_agent") or {}
    state = _read(app_dir / FORM_STATE)
    final = state.get("fields") or {}
    if not baseline or not final:
        return []
    if store is None:
        store = form_profile.load_corrections().corrections
    on_file = {form_profile.normalise_label(k): r.get("value", "") for k, r in store.items()}
    out = []
    for label, value in diff(baseline, final).items():
        out.append({
            "label": label,
            "was": str(baseline.get(label, "") or "").strip(),
            "now": value,
            "remembered": on_file.get(form_profile.normalise_label(label)) == value,
        })
    return out


def remember(app_dir: Path, labels: list[str], profile_path: Optional[Path] = None) -> dict:
    """The human picked which of the changes to keep: those and only
    those go into the store, with what the form held and the control
    they were seen on. Nothing is recorded that was not picked."""
    wanted = {form_profile.normalise_label(l) for l in labels if str(l).strip()}
    if not wanted:
        return {"remembered": 0, "reason": "nothing picked"}
    rows = [c for c in changes(app_dir, store={}) if form_profile.normalise_label(c["label"]) in wanted]
    if not rows:
        return {"remembered": 0, "reason": "none of those changed"}
    report = _read(app_dir / FILL_REPORT)
    state = _read(app_dir / FORM_STATE)
    meta = state.get("meta") or report.get("after_agent_meta") or {}
    system = str((report.get("report") or {}).get("ats") or "") or ats.detect(str(report.get("url") or ""))
    picked = {c["label"]: c["now"] for c in rows}
    before = {c["label"]: c["was"] for c in rows}
    written = form_profile.add_corrections(picked, profile_path, system=system, fields=meta,
                                           job=app_dir.name, before=before)
    lines = ["# Corrections", "", "Picked by hand on the form; each is now in `base/form.json` under",
             "`corrections` and goes over autofill on that field next time.", ""]
    for c in rows:
        lines.append(f"- **{c['label']}**: {c['was']!r} → {c['now']!r}" if c["was"] else f"- **{c['label']}**: {c['now']!r}")
    try:
        existing = (app_dir / NOTES).read_text() if (app_dir / NOTES).exists() else ""
        (app_dir / NOTES).write_text((existing.rstrip() + "\n\n" if existing else "") + "\n".join(lines) + "\n")
    except OSError:
        pass
    return {"remembered": written, "changes": picked}
