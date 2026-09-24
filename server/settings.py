"""Reviewer-facing switches, kept in data/settings.json.

One file, read by the server and by the scripts it launches, so a switch
flipped on the page is seen by the next pipeline run without a restart.
Nothing here is secret or personal; secrets stay in .env.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

import paths

ROOT = Path(__file__).resolve().parent.parent
SETTINGS_PATH = Path(os.environ.get("AUTOPILOT_SETTINGS", paths.DATA / "settings.json"))

# use_profile: feed the applicant's facts and story documents to the models.
# Off, or on with nothing written yet, means the resume alone, which is how
# the pipeline ran before the profile existed.
# auto_fill: once the tailoring is done and the screen did not reject, mark
# the job approved and start the browser fill without waiting at checkpoint
# 1. The clean verdict already queued the job by itself; the human's decision
# moves to checkpoint 2, the filled form. A poor-fit verdict still waits.
# auto_learn: the correction loop. On, the review page and the banner list
# what the human changed on a filled form, Remember puts it on file, and the
# next fill writes the corrected values over autofill's. Off since 2026-09-21:
# Jobright fixed the autofill it was correcting for, so the rows are noise
# and nothing on file is applied. The table on the Profile tab stays
# readable either way.
# learn_prompts: the Workshop reads the re-tailor requests across jobs once a
# few new ones have piled up, and proposes prompt edits for what the person
# keeps asking for. A proposal is never applied without their click.
# scout_checks_per_day: how often every watched careers page is read, unless
# the watch sets its own. 4 = every six hours.
DEFAULTS = {"use_profile": True, "auto_fill": True, "auto_learn": False, "learn_prompts": True, "scout_checks_per_day": 4}

router = APIRouter()


class Settings(BaseModel):
    use_profile: Optional[bool] = None
    auto_fill: Optional[bool] = None
    auto_learn: Optional[bool] = None
    learn_prompts: Optional[bool] = None
    scout_checks_per_day: Optional[int] = None


def load() -> dict:
    data = dict(DEFAULTS)
    if SETTINGS_PATH.exists():
        try:
            data.update(json.loads(SETTINGS_PATH.read_text() or "{}"))
        except json.JSONDecodeError:
            pass
    return data


def save(**changes) -> dict:
    data = load() | changes
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(data, indent=2) + "\n")
    return data


def use_profile() -> bool:
    return bool(load().get("use_profile", True))


def auto_fill() -> bool:
    return bool(load().get("auto_fill", True))


def auto_learn() -> bool:
    return bool(load().get("auto_learn", False))


@router.get("/settings")
def get_settings() -> dict:
    return load()


@router.post("/settings")
def set_settings(update: Settings) -> dict:
    # Only the keys sent change; the page posts one switch at a time.
    return save(**update.model_dump(exclude_none=True))
