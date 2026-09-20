"""POST /screen: the on-page auto-reject check, with a URL-keyed cache.

The extension calls this the moment a job page opens, and the pipeline calls
the same function again when the job is captured. The cache means the model
runs once per posting however many times the page is opened. Nothing here
changes a job's status: the verdict is advice for the person reading it.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from tailor import quality
from tailor import screen as screener

from . import postings, seen

ROOT = Path(__file__).resolve().parent.parent
CACHE_PATH = Path(os.environ.get("AUTOPILOT_SCREENS", ROOT / "data" / "screens.json"))

router = APIRouter()


class ScreenRequest(BaseModel):
    url: str
    text: str
    title: str = ""
    force: bool = False


def cache_key(url: str) -> str:
    """The URL without tracking noise, so Jobright's ?ref= and the bare link match."""
    url = re.sub(r"#.*$", "", url)
    url = re.sub(r"[?&](utm_[a-z]+|ref|source|src|gh_src|lever-source|jobright[a-z_]*)=[^&]*", "", url)
    url = re.sub(r"\?$", "", url)
    return url.rstrip("/")


def _read() -> dict:
    if not CACHE_PATH.exists():
        return {}
    try:
        return json.loads(CACHE_PATH.read_text() or "{}")
    except json.JSONDecodeError:
        return {}


def _write(data: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=CACHE_PATH.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(data, indent=2))
        os.replace(tmp, CACHE_PATH)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def cached(url: str) -> Optional[screener.Screen]:
    entry = _read().get(cache_key(url))
    return screener.Screen.from_dict(entry) if entry else None


def remember(url: str, result: screener.Screen) -> None:
    data = _read()
    data[cache_key(url)] = result.to_dict()
    _write(data)


def screen_url(url: str, text: str, title: str = "", force: bool = False) -> tuple[screener.Screen, bool]:
    """Screen a posting, from cache when possible. Returns (result, was_cached).

    A `not_a_job` is not cached: it usually means the page had not finished
    rendering when the text was taken, and the next open should try again.
    """
    source = postings.source_for(url)
    if not force:
        hit = cached(url)
        if hit is not None:
            return hit, True
        # Jobright and the employer page are two URLs for the same job.  The
        # former is normally screened first and carries the full description;
        # reuse that verdict instead of paying to classify a bare ATS shell.
        if source is not None:
            hit = cached(source.url)
            if hit is not None:
                remember(url, hit)
                return hit, True

    screen_text = text
    screen_title = title
    if source is not None:
        page_score = quality.score(text)
        source_score = quality.score(source.text)
        if source_score and page_score < source_score * quality.GOOD_ENOUGH:
            # Keep both representations in the single model request.  Put the
            # known posting first so the screener's input cap cannot discard it.
            screen_text = (
                "## Jobright posting copy\n\n" + source.text.strip()
                + ("\n\n## Employer page copy\n\n" + text.strip() if text.strip() else "")
            )
            screen_title = source.title or title

    result = screener.screen(screen_text, title=screen_title, url=url)
    if result.verdict != "not_a_job":
        remember(url, result)
    return result, False


@router.post("/screen")
def screen_endpoint(req: ScreenRequest) -> dict:
    try:
        result, was_cached = screen_url(req.url, req.text, title=req.title, force=req.force)
    except screener.ScreenError as exc:
        raise HTTPException(422, str(exc))
    except Exception as exc:  # model or network; the banner shows the message
        raise HTTPException(502, f"screen failed: {exc}")
    # Already in autopilot? Asked after the verdict so a failed lookup can
    # never cost the screen; string work only, no model.
    try:
        match = seen.find(req.url, title=req.title, text=req.text)
    except Exception:  # noqa: BLE001 - advice on the page, never an error
        match = None
    return result.to_dict() | {"cached": was_cached, "seen": match.to_dict() if match else None}
