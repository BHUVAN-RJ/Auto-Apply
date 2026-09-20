"""What the deterministic filler knows about the applicant: `base/form.json`.

Structured on purpose. `base/applicant.md` is prose for the models; a
filler that never calls a model needs the first name in a field called
first_name. The file is gitignored (the repo is public), the template is
`base/form.example.json`, and a missing file means the code filler steps
aside and the fill runs as it did before.

`corrections` is the correction store: a question's label, as the form
shows it, to a record of what the human changed it to (`value`), what the
form held before (`was`, usually Jobright's autofill), which system and
field it was seen on, and when. It is checked before every heuristic, so
a value the human put there wins over anything the mapping guesses, and
`Engine.apply_corrections` writes it over whatever autofill left on the
next form that asks the same question. The older `answers` key (label to
plain value) is read as a record with no `was` and rewritten as
`corrections` on the next save.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent.parent
FORM = ROOT / "base" / "form.json"

# Keys the adapters and the label mapping can name. Anything else in the
# file is ignored, so a typo is a blank field rather than a crash.
KEYS = (
    "first_name", "last_name", "preferred_name", "full_name", "email", "phone",
    "phone_country", "linkedin", "github", "website", "twitter", "address",
    "city", "state", "postal_code", "country", "location", "current_company",
    "current_title", "school", "degree", "discipline", "graduation_year",
    "how_heard", "salary", "start_date", "gender", "race", "veteran", "disability",
)


def normalise_label(label: str) -> str:
    """A label as a key: lowercased, no punctuation, no required marker,
    single spaces. "Are you willing to relocate? *" and "are you willing to
    relocate" are the same question."""
    text = (label or "").lower()
    text = re.sub(r"\(required\)|\(optional\)|\*", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


class Profile:
    def __init__(self, data: dict):
        self.values: dict[str, str] = {}
        for key in KEYS:
            value = data.get(key)
            if isinstance(value, (str, int, float)) and str(value).strip():
                self.values[key] = str(value).strip()
        first, last = self.values.get("first_name", ""), self.values.get("last_name", "")
        if "full_name" not in self.values and (first or last):
            self.values["full_name"] = " ".join(p for p in (first, last) if p)
        if "location" not in self.values:
            parts = [self.values.get(k, "") for k in ("city", "state", "country")]
            if any(parts):
                self.values["location"] = ", ".join(p for p in parts if p)
        self.corrections: dict[str, dict] = corrections_of(data)
        self.answers: dict[str, str] = {
            normalise_label(k): r["value"] for k, r in self.corrections.items()
        }

    def get(self, key: str) -> str:
        return self.values.get(key, "")

    def answer(self, label: str) -> str:
        return self.answers.get(normalise_label(label), "")

    def correction(self, label: str) -> Optional[dict]:
        key = normalise_label(label)
        return next((r for k, r in self.corrections.items() if normalise_label(k) == key), None)

    def __bool__(self) -> bool:
        return bool(self.values)


def load(path: Optional[Path] = None) -> Optional[Profile]:
    """The profile, or None when the file is missing or unreadable. The
    path is resolved here, not in a default argument, so a test can point
    it elsewhere."""
    path = path or FORM
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    profile = Profile(data)
    return profile if profile else None


def _record(value, was: str = "", **extra) -> Optional[dict]:
    """One correction as stored: a plain string from the old `answers`
    key, or a record. Empty values are not corrections."""
    if isinstance(value, dict):
        text = str(value.get("value", "") or "").strip()
        if not text:
            return None
        record = {k: v for k, v in value.items() if k != "value"}
        record["value"] = text
        record.setdefault("was", "")
        return record
    text = str(value or "").strip()
    if not text:
        return None
    return {"value": text, "was": str(was or "").strip(), **extra}


def corrections_of(data: dict) -> dict[str, dict]:
    """The correction store out of a form.json dict: `corrections` first,
    the legacy `answers` key folded in behind it, `_comment` and blanks
    dropped, every entry a record."""
    out: dict[str, dict] = {}
    for key in ("answers", "corrections"):
        raw = data.get(key)
        if not isinstance(raw, dict):
            continue
        for label, value in raw.items():
            if not isinstance(label, str) or label.startswith("_") or not label.strip():
                continue
            record = _record(value)
            if record:
                out[label.strip()] = record
    return out


def load_corrections(path: Optional[Path] = None) -> Profile:
    """A profile holding only the correction store, for the pass that runs
    after Jobright's autofill: no contact details, so nothing but an
    exact label match ever writes anything. Empty when the file is
    missing or unreadable, never None."""
    path = path or FORM
    try:
        data = json.loads(path.read_text()) if path.exists() else {}
    except (OSError, ValueError):
        data = {}
    return Profile({"corrections": corrections_of(data if isinstance(data, dict) else {})})


def add_corrections(changes: dict, path: Optional[Path] = None, system: str = "",
                    fields: Optional[dict] = None, job: str = "", before: Optional[dict] = None) -> int:
    """Record corrections: question label -> the value the human put on
    the form. `before` is what the form held per label (Jobright's value,
    usually), `fields` the identifiers the scan saw per label (id, name,
    kind), `system` the ATS. Merged into `corrections` in base/form.json,
    created when the file is missing, existing entries overwritten (the
    newest correction is the right one); a legacy `answers` key is folded
    in and removed. Returns how many were written."""
    path = path or FORM
    data: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
    corrections = corrections_of(data)
    written = 0
    when = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for label, value in changes.items():
        label = str(label).strip()
        record = _record(value, (before or {}).get(label, ""))
        if not label or not record:
            continue
        record.setdefault("system", system)
        record.setdefault("field", (fields or {}).get(label) or {})
        record.setdefault("when", when)
        if job:
            record.setdefault("job", job)
        # The same question with different punctuation is one entry.
        for existing in list(corrections):
            if normalise_label(existing) == normalise_label(label) and existing != label:
                del corrections[existing]
        corrections[label] = record
        written += 1
    if written or "answers" in data:
        data.pop("answers", None)
        data["corrections"] = corrections
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return written
