"""What the deterministic filler knows about the applicant: `base/form.json`.

Structured on purpose. `base/applicant.md` is prose for the models; a
filler that never calls a model needs the first name in a field called
first_name. The file is gitignored (the repo is public), the template is
`base/form.example.json`, and a missing file means the code filler steps
aside and the fill runs as it did before.

`answers` is the correction store: a question's label, as the form shows
it, to the answer. It is checked before every heuristic, so a value the
human put there wins over anything the mapping guesses.
"""
from __future__ import annotations

import json
import re
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
        answers = data.get("answers") or {}
        self.answers: dict[str, str] = {
            normalise_label(k): str(v).strip()
            for k, v in answers.items()
            if isinstance(k, str) and not k.startswith("_") and str(v).strip()
        }

    def get(self, key: str) -> str:
        return self.values.get(key, "")

    def answer(self, label: str) -> str:
        return self.answers.get(normalise_label(label), "")

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


def add_answers(changes: dict, path: Optional[Path] = None) -> int:
    """Record corrections: question label -> the value the human put on
    the form. Merged into `answers` in base/form.json, created when the
    file is missing, existing values overwritten (the newest correction is
    the right one). Returns how many were written."""
    path = path or FORM
    data: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
    answers = data.get("answers")
    if not isinstance(answers, dict):
        answers = {}
    written = 0
    for label, value in changes.items():
        label, value = str(label).strip(), str(value).strip()
        if not label or not value:
            continue
        # The same question with different punctuation is one entry.
        for existing in list(answers):
            if not existing.startswith("_") and normalise_label(existing) == normalise_label(label) and existing != label:
                del answers[existing]
        answers[label] = value
        written += 1
    if written:
        data["answers"] = answers
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return written
