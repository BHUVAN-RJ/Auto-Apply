"""Reviewer-facing switches, kept in data/settings.json.

One file, read by the server and by the scripts it launches, so a switch
flipped on the page is seen by the next pipeline run without a restart.
Nothing here is secret or personal; secrets stay in .env.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
SETTINGS_PATH = Path(os.environ.get("AUTOPILOT_SETTINGS", ROOT / "data" / "settings.json"))

# use_profile: feed the applicant's facts and story documents to the models.
# Off, or on with nothing written yet, means the resume alone, which is how
# the pipeline ran before the profile existed.
# auto_fill: once the tailoring is done and the screen did not reject, mark
# the job approved and start the browser fill without waiting at checkpoint
# 1. The clean verdict already queued the job by itself; the human's decision
# moves to checkpoint 2, the filled form. A poor-fit verdict still waits.
DEFAULTS = {"use_profile": True, "auto_fill": True}

router = APIRouter()


class Settings(BaseModel):
    use_profile: bool = True
    auto_fill: bool = True


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


@router.get("/settings")
def get_settings() -> dict:
    return load()


@router.post("/settings")
def set_settings(update: Settings) -> dict:
    return save(**update.model_dump())
