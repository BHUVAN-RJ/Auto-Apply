"""The preliminary interview: the fixed questions every application form
asks, answered once, click-through, no model.

Writes `base/form.json`, which `browser/forms` fills forms from. The
questions are a list here so the page and the file agree on the keys;
the page renders one at a time. Work-authorisation questions are asked
too (status, sponsorship, dates) because every form asks them and the
answers belong on file, but the filler never puts them on a form: those
fields are refused by `guard.describes_protected` before matching, and
the human answers them on the page. `corrections` (the correction store,
`browser/forms/profile.py`) is kept across saves and shown as a table
for deleting.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from browser.forms import profile as form_profile
from tailor import profile as tailor_profile

router = APIRouter(prefix="/profile/form")

DECLINE = "Decline to self-identify"
YES_NO = ["Yes", "No"]

# kind: text | select | yesno | note. `options` for select. `fixed` marks
# an answer the page shows but does not ask. `protected` marks the
# authorisation block: collected, never auto-filled.
QUESTIONS: list[dict] = [
    {"section": "Contact"},
    {"key": "first_name", "ask": "First name", "kind": "text"},
    {"key": "last_name", "ask": "Last name", "kind": "text"},
    {"key": "preferred_name", "ask": "Preferred first name, if different", "kind": "text"},
    {"key": "email", "ask": "Email for applications", "kind": "text"},
    {"key": "phone", "ask": "Phone number, digits only", "kind": "text", "help": "Forms add the +1 themselves."},
    {"key": "linkedin", "ask": "LinkedIn URL", "kind": "text"},
    {"key": "github", "ask": "GitHub URL", "kind": "text"},
    {"key": "website", "ask": "Portfolio or personal site URL", "kind": "text"},
    {"key": "twitter", "ask": "Twitter / X URL", "kind": "text"},
    {"section": "Location"},
    {"key": "address", "ask": "Street address", "kind": "text"},
    {"key": "city", "ask": "City", "kind": "text"},
    {"key": "state", "ask": "State, spelled out", "kind": "text", "help": "Texas, not TX: forms with a list match the full name."},
    {"key": "postal_code", "ask": "ZIP / postal code", "kind": "text"},
    {"key": "country", "ask": "Country", "kind": "text", "default": "United States"},
    {"key": "phone_country", "ask": "Phone country", "kind": "text", "default": "United States"},
    {"section": "Work"},
    {"key": "current_company", "ask": "Current or most recent employer", "kind": "text"},
    {"key": "current_title", "ask": "Current or most recent title", "kind": "text"},
    {"key": "salary", "ask": "Salary expectation, as you would type it on a form", "kind": "text",
     "help": "Leave empty to have the agent leave it blank."},
    {"key": "start_date", "ask": "Earliest start date", "kind": "text", "default": "Immediately"},
    {"section": "Education"},
    {"key": "school", "ask": "School, as the form should read it", "kind": "text"},
    {"key": "degree", "ask": "Highest degree", "kind": "select",
     "options": ["High School", "Associate's Degree", "Bachelor's Degree", "Master's Degree", "Doctorate (PhD)", "Other"]},
    {"key": "discipline", "ask": "Field of study", "kind": "text"},
    {"key": "graduation_year", "ask": "Graduation year (most recent degree)", "kind": "text"},
    {"section": "Source"},
    {"key": "how_heard", "ask": "How did you hear about us?", "kind": "text", "default": "Other", "fixed": True,
     "help": "Always \"Other\", never the job board. Shown, not asked."},
    {"section": "Self-identification (optional; every form offers a decline)"},
    {"key": "gender", "ask": "Gender", "kind": "select", "options": ["Male", "Female", "Non-binary", DECLINE]},
    {"key": "race", "ask": "Race / ethnicity", "kind": "select",
     "options": ["Asian", "Black or African American", "Hispanic or Latino", "White",
                 "Native American or Alaska Native", "Native Hawaiian or Other Pacific Islander", "Two or More Races", DECLINE]},
    {"key": "veteran", "ask": "Veteran status", "kind": "select",
     "options": ["I am not a protected veteran", "I identify as one or more of the classifications of a protected veteran", DECLINE]},
    {"key": "disability", "ask": "Disability status", "kind": "select",
     "options": ["No, I do not have a disability", "Yes, I have a disability (or previously had one)", DECLINE]},
    {"section": "Work authorisation (on file for you and the screen; never typed onto a form by the filler)"},
    {"key": "citizenship", "ask": "Country of citizenship", "kind": "text", "protected": True},
    {"key": "us_status", "ask": "Current US status", "kind": "select", "protected": True,
     "options": ["US citizen", "Permanent resident (green card)", "F-1 student", "F-1 CPT", "F-1 OPT", "F-1 STEM OPT",
                 "H-1B", "H-4 EAD", "L-1", "TN", "O-1", "J-1", "Other"]},
    {"key": "work_authorized", "ask": "Authorised to work in the US today?", "kind": "yesno", "protected": True},
    {"key": "needs_sponsorship", "ask": "Will you need visa sponsorship now or in the future?", "kind": "yesno", "protected": True},
    {"key": "authorization_expires", "ask": "When does your current authorisation (OPT / EAD / visa) end?", "kind": "text",
     "protected": True, "help": "Month and year is enough. Empty if it does not."},
    {"key": "clearance", "ask": "Security clearance", "kind": "select", "protected": True,
     "options": ["None", "None, and not eligible", "Public Trust", "Secret", "Top Secret", "TS/SCI"]},
]

KEYS = [q["key"] for q in QUESTIONS if "key" in q]


class Save(BaseModel):
    values: dict
    corrections: Optional[dict] = None


def _path() -> Path:
    return form_profile.FORM


def _read(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


@router.get("")
def get_form() -> dict:
    """The questions, what is on file, and whether the file exists."""
    data = _read(_path())
    values = {k: str(data.get(k, "")).strip() for k in KEYS if str(data.get(k, "")).strip()}
    return {"questions": QUESTIONS, "values": values, "corrections": form_profile.corrections_of(data),
            "exists": _path().exists(), "answered": len(values), "total": len(KEYS)}


@router.get("/hints")
def hints() -> dict:
    """Contact details the resume already carries, to prefill the page.
    One cheap model call, cached next to the derived facts."""
    from tailor import facts

    cache = tailor_profile.DERIVED_FACTS.with_name("contacts.json")
    if cache.exists():
        try:
            return {"hints": json.loads(cache.read_text())}
        except (OSError, ValueError):
            pass
    if not tailor_profile.BASE_RESUME.exists():
        return {"hints": {}}
    try:
        found = facts.contacts()
    except Exception as error:  # noqa: BLE001 - hints are a convenience
        raise HTTPException(502, f"could not read contacts from the resume: {error}")
    mapped = {}
    for label, value in found.items():
        key = {"phone": "phone", "email": "email", "linkedin": "linkedin", "github": "github",
               "website": "website", "portfolio": "website", "name": "full_name",
               "location": "location", "city": "city"}.get(label.strip().lower())
        if key:
            mapped[key] = value
    if "full_name" in mapped and "first_name" not in mapped:
        parts = mapped.pop("full_name").split()
        if parts:
            mapped["first_name"], mapped["last_name"] = parts[0], " ".join(parts[1:])
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(mapped, indent=1))
    except OSError:
        pass
    return {"hints": mapped}


@router.post("")
def save(body: Save) -> dict:
    """Write base/form.json: the asked keys from `values`, `corrections`
    replaced when sent (a plain string is a record with no `was`),
    everything else in the file kept; a legacy `answers` key is folded
    into `corrections` either way."""
    path = _path()
    data = _read(path)
    for key in KEYS:
        value = str(body.values.get(key, "") or "").strip()
        if value:
            data[key] = value
        else:
            data.pop(key, None)
    data["how_heard"] = "Other"
    if body.corrections is not None:
        data["corrections"] = form_profile.corrections_of({"corrections": body.corrections})
    else:
        data["corrections"] = form_profile.corrections_of(data)
    data.pop("answers", None)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return get_form()
