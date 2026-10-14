"""Where the slugs come from: the public new-grad and internship lists on
GitHub, read once and mined for board links.

Those repositories exist to list postings, and every row carries the
employer's own apply link. A link to Greenhouse, Lever or Ashby contains
the company's slug, and `scout.detect` reads it out of every URL shape
those boards use - the hosted page, the embedded form, the `?for=` query.
So a list nobody maintains any more is still worth reading: the postings
are dead, the companies and their boards are not.

Two shapes. Some repositories publish the whole list as JSON
(`.github/scripts/listings.json`), which also carries the company's name;
the rest render it as a README of tables, where the links are all there is.
Both are read with the crudest possible parser - every `http(s)` string in
the file - because a schema we do not control is not a thing to depend on,
and a link that is not a board link is simply refused by `detect`.

Measured on the first harvest (2026-10-14): 80,786 links across nine
files, 1,979 distinct companies. One list was dropped after measuring it:
`jobright-ai/2025-Software-Engineer-New-Grad` links to its own site rather
than to employers' boards, so it yielded nothing.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Iterator

import httpx

from boardscan import SYSTEMS, registry
from scout import DetectError, detect
from scout.providers import USER_AGENT

TIMEOUT = 120.0          # a 13 MB listings.json over a home connection
RAW = "https://raw.githubusercontent.com/{repo}/{ref}/{path}"
LINK = re.compile(r"https?://[^\s)\"'<>\]]+")


@dataclass(frozen=True)
class Source:
    """One file in one repository, and what it is worth.

    `name` is what the page and the registry call it. `measured` is the
    distinct companies it contributed on the first harvest, kept here so a
    source that quietly stops carrying links is obvious in the numbers
    rather than invisible.
    """

    name: str
    repo: str
    ref: str
    path: str
    measured: int = 0

    @property
    def url(self) -> str:
        return RAW.format(repo=self.repo, ref=self.ref, path=self.path)


SOURCES = (
    Source("simplify-newgrad", "SimplifyJobs/New-Grad-Positions", "dev",
           ".github/scripts/listings.json", measured=2930),
    Source("simplify-internships", "SimplifyJobs/Summer2026-Internships", "dev",
           ".github/scripts/listings.json", measured=2820),
    Source("vansh-newgrad", "vanshb03/New-Grad-2026", "dev",
           ".github/scripts/listings.json", measured=223),
    Source("vansh-internships", "vanshb03/Summer2026-Internships", "dev",
           ".github/scripts/listings.json", measured=151),
    Source("speedyapply", "speedyapply/2026-SWE-College-Jobs", "main",
           "README.md", measured=250),
    Source("arunike", "arunike/Summer-2025-Internship-List", "main",
           "README.md", measured=221),
    Source("reavnail-2024", "ReaVNaiL/New-Grad-2024", "main",
           "README.md", measured=51),
    Source("ouckah-2025", "Ouckah/Summer2025-Internships", "main",
           "README.md", measured=46),
)


def client() -> httpx.Client:
    return httpx.Client(follow_redirects=True, timeout=TIMEOUT,
                        headers={"User-Agent": USER_AGENT})


def _strings(value) -> Iterator[str]:
    """Every string anywhere in a parsed JSON document.

    The rows carry the apply link under `url` on these lists today, and
    under something else tomorrow; walking the whole document costs
    nothing and cannot be broken by a renamed field.
    """
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def links(text: str) -> Iterator[tuple[str, str]]:
    """`(link, company name)` out of one downloaded file.

    A JSON list of rows is read row by row so the company's own name can
    travel with its link; anything else, JSON or markdown, is read as text.
    """
    body = text.lstrip()
    if body.startswith("[") or body.startswith("{"):
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, list):
            for row in data:
                if isinstance(row, dict):
                    name = str(row.get("company_name") or row.get("company") or "")
                    for s in _strings(row):
                        if s.startswith("http"):
                            yield s, name
                else:
                    for s in _strings(row):
                        if s.startswith("http"):
                            yield s, ""
            return
        if data is not None:
            for s in _strings(data):
                if s.startswith("http"):
                    yield s, ""
            return
    for link in LINK.findall(text):
        yield link, ""


def slugs(text: str) -> tuple[list[tuple[str, str, str]], int]:
    """`[(system, slug, name)]` and how many links were looked at.

    `detect` refuses everything that is not one of the systems we read,
    which is most of the links in these files - LinkedIn, Workday, the
    repositories' own pages, badges, images.
    """
    out, seen, found = [], 0, set()
    for link, name in links(text):
        seen += 1
        try:
            system, args, guess = detect(link)
        except (DetectError, ValueError):
            continue
        except Exception:                        # noqa: BLE001 - a malformed URL is not news
            continue
        if system not in SYSTEMS:
            continue
        slug = (args.get("slug") or "").strip().strip("/").lower()
        if not slug or (system, slug) in found:
            continue
        found.add((system, slug))
        out.append((system, slug, (name or guess or "").strip()))
    return out, seen


def harvest_one(source: Source, http: httpx.Client | None = None) -> dict:
    """Read one list and fold its companies into the registry.

    Raises on a failed download: a source that has moved should be visible
    as an error, not as a harvest that found nothing.
    """
    own = http is None
    http = http or client()
    try:
        text = http.get(source.url).raise_for_status().text
    finally:
        if own:
            http.close()
    rows, seen = slugs(text)
    found = {s: sum(1 for r in rows if r[0] == s) for s in SYSTEMS}
    counts = registry.add_many((system, slug, name, source.name) for system, slug, name in rows)
    registry.record_harvest(source.name, seen, found, counts)
    return {"source": source.name, "links": seen, "found": found, **counts}


def harvest(only: list[str] | None = None) -> dict:
    """Every source, or the named ones. One list's failure is recorded and
    the rest still run: eight lists are eight independent bets."""
    out, errors = [], {}
    http = client()
    try:
        for source in SOURCES:
            if only and source.name not in only:
                continue
            try:
                out.append(harvest_one(source, http))
            except Exception as e:               # noqa: BLE001 - reported, never raised
                errors[source.name] = f"{type(e).__name__}: {e}"
    finally:
        http.close()
    return {"sources": out, "errors": errors, "registry": registry.stats()}
