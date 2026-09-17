"""Posting text kept from the browser, for when the fetch gets nothing.

Some Apply buttons land straight on the application form, with no
description on the page at all, and many ATS pages render client-side, so
`tailor.fetch` sees an empty shell. The browser had the text in both cases:
the Jobright posting the person read before clicking Apply, and whatever the
employer page rendered. Both are kept here, under `data/postings/`, and the
pipeline falls back to them in that order of preference: the employer page
first, as it is the real posting, then Jobright's copy.

Two keys, because the two pages are known at different times:

- `source/<posting id>` is Jobright's own text, saved when its posting page
  is screened. The id is the one in `/jobs/info/<id>`, which Jobright also
  tags onto the URL it opens (`?jr_id=<id>`), so the capture can find it.
- `captured/<job id>` is the employer page's text, sent with the capture.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional

from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
DIR_ENV = "AUTOPILOT_POSTINGS"

SOURCE_ID = re.compile(r"[?&]jr_id=([A-Za-z0-9_-]+)")
# Jobright titles its posting pages "<role> @ <company> | Jobright.ai".
SOURCE_TITLE = re.compile(r"^(?P<title>.+?)\s+@\s+(?P<company>.+?)\s*\|")


class Saved(BaseModel):
    url: str
    title: str = ""
    text: str = ""
    company: str = ""


def directory() -> Path:
    return Path(os.environ.get(DIR_ENV, ROOT / "data" / "postings"))


def _safe(key: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", key)[:80]


def _path(kind: str, key: str) -> Path:
    return directory() / kind / f"{_safe(key)}.json"


def _write(kind: str, key: str, saved: Saved) -> None:
    path = _path(kind, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(saved.model_dump(), indent=2))
    os.replace(tmp, path)


def _read(kind: str, key: str) -> Optional[Saved]:
    path = _path(kind, key)
    if not path.exists():
        return None
    try:
        return Saved.model_validate_json(path.read_text())
    except ValueError:
        return None


def source_id(url: str) -> Optional[str]:
    """The Jobright posting id an employer URL was opened for, if tagged."""
    match = SOURCE_ID.search(url)
    return match.group(1) if match else None


def save_source(posting_id: str, saved: Saved) -> None:
    match = SOURCE_TITLE.match(saved.title)
    if match:
        saved = saved.model_copy(update={"title": match["title"],
                                         "company": saved.company or match["company"]})
    _write("source", posting_id, saved)


def save_captured(job_id: str, saved: Saved) -> None:
    _write("captured", job_id, saved)


def fallbacks(job_id: str, url: str) -> list[Saved]:
    """Texts to try when the fetch has nothing, best first."""
    found = []
    captured = _read("captured", job_id)
    posting_id = source_id(url)
    source = _read("source", posting_id) if posting_id else None
    if captured and captured.text.strip():
        if source and not captured.company:
            # The employer page rarely says who it is; Jobright's title does.
            captured = captured.model_copy(update={"company": source.company})
        found.append(captured)
    if source and source.text.strip():
        found.append(source)
    return found
