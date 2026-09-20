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
SOURCE_POSTED = re.compile(
    r"^(?:reposted\s+)?(?:\d+\s+)?(?:minutes?|hours?|days?|weeks?|months?)\s+ago$|^just now$",
    re.I,
)


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


def _source_metadata(saved: Saved) -> tuple[str, str]:
    """(role, company) from Jobright's title or its posting header.

    Jobright currently leaves ``document.title`` at "Job Recommendations |\n+    Jobright AI".  Its visible posting still has a stable header immediately
    after "Original Job Post": company, age, then role.
    """
    match = SOURCE_TITLE.match(saved.title)
    if match:
        return match["title"], saved.company or match["company"]

    lines = [line.strip() for line in saved.text.splitlines() if line.strip()]
    marker = next((i for i, line in enumerate(lines) if line.casefold() == "original job post"), None)
    if marker is not None:
        after = lines[marker + 1:]
        company = next((line for line in after if line != "·"), "")
        posted = next((i for i, line in enumerate(after) if SOURCE_POSTED.match(line)), None)
        if posted is not None:
            title = next((line for line in after[posted + 1:] if line != "·"), "")
            if title:
                return title, saved.company or company
    return saved.title, saved.company


def save_source(posting_id: str, saved: Saved) -> None:
    title, company = _source_metadata(saved)
    if (title, company) != (saved.title, saved.company):
        saved = saved.model_copy(update={"title": title, "company": company})
    _write("source", posting_id, saved)


def source_meta(url: str) -> tuple[str, str]:
    """(title, company) from Jobright's copy of the posting the URL was
    opened for. Jobright's page title names the role and the employer
    cleanly; the employer page's title is whatever its ATS put there."""
    source = source_for(url)
    if not source:
        return "", ""
    return source.title, source.company


def source_for(url: str) -> Optional[Saved]:
    """Jobright's saved copy for an employer URL carrying its posting id."""
    posting_id = source_id(url)
    source = _read("source", posting_id) if posting_id else None
    if source is None:
        return None
    title, company = _source_metadata(source)
    return source.model_copy(update={"title": title, "company": company})


def save_captured(job_id: str, saved: Saved) -> None:
    _write("captured", job_id, saved)


def fallbacks(job_id: str, url: str) -> list[Saved]:
    """Texts to try when the fetch has nothing, best first."""
    found = []
    captured = _read("captured", job_id)
    source = source_for(url)
    if captured and captured.text.strip():
        if source:
            # The employer page rarely says who it is, and titles itself
            # however its ATS likes; Jobright's title names both cleanly.
            captured = captured.model_copy(update={
                "company": captured.company or source.company,
                "title": source.title or captured.title})
        found.append(captured)
    if source and source.text.strip():
        found.append(source)
    return found
