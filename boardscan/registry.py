"""The slug registry: `data/boardscan.json`, one file, written whole and
atomically, the same shape as `scout/store.py` and for the same reason -
two thousand rows of three short strings is nothing to re-read, and a file
a person can open and edit is worth more here than a schema.

A slug is a guess until the board answers. `validate` is what turns a
harvested name into a company we read every day: one request, and a 404 is
recorded as `dead` rather than deleted, so the next harvest does not offer
the same dead name back as news.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Iterable, Optional

import paths
from boardscan import SYSTEMS, Company
from server.models import utcnow

ENV = "AUTOPILOT_BOARDSCAN_FILE"

_lock = threading.RLock()


def path() -> Path:
    """Resolved per call, never frozen at import: a test points `ENV` at a
    temporary file, and the installed app's data directory is not the
    clone's."""
    return Path(os.environ.get(ENV) or paths.DATA / "boardscan.json")


def _read() -> dict:
    p = path()
    if not p.exists():
        return {"companies": [], "harvests": []}
    try:
        data = json.loads(p.read_text() or "{}")
    except json.JSONDecodeError:
        return {"companies": [], "harvests": []}
    data.setdefault("companies", [])
    data.setdefault("harvests", [])
    return data


def _write(data: dict) -> None:
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1) + "\n")
    os.replace(tmp, p)


def companies(system: str = "", include_dead: bool = False) -> list[Company]:
    with _lock:
        rows = [Company(**c) for c in _read()["companies"]]
    if system:
        rows = [c for c in rows if c.system == system]
    if not include_dead:
        rows = [c for c in rows if not c.dead]
    return rows


def get(company_id: str) -> Optional[Company]:
    return next((c for c in companies(include_dead=True) if c.id == company_id), None)


def add(system: str, slug: str, name: str = "", source: str = "") -> tuple[Company, bool]:
    """The company, and whether it is new. An existing row gains the source
    and, if it had none, the name: a slug in three lists is one company
    that three lists know about."""
    if system not in SYSTEMS:
        raise ValueError(f"not a board we read: {system}")
    slug = slug.strip().strip("/").lower()
    if not slug:
        raise ValueError("empty slug")
    with _lock:
        data = _read()
        for row in data["companies"]:
            if row["system"] == system and row["slug"] == slug:
                existing = Company(**row)
                changed = False
                if source and source not in existing.sources:
                    existing.sources.append(source)
                    changed = True
                if name and not existing.name:
                    existing.name = name
                    changed = True
                if changed:
                    row.update(existing.model_dump())
                    _write(data)
                return existing, False
        fresh = Company(system=system, slug=slug, name=name,
                        sources=[source] if source else [])
        data["companies"].append(fresh.model_dump())
        _write(data)
        return fresh, True


def add_many(rows: Iterable[tuple[str, str, str, str]]) -> dict:
    """`(system, slug, name, source)` in bulk, in one read and one write.

    A harvest offers tens of thousands of links and `add` one at a time
    would rewrite the file for each.
    """
    counts = {"new": 0, "known": 0, "refused": 0}
    with _lock:
        data = _read()
        index = {(c["system"], c["slug"]): c for c in data["companies"]}
        for system, slug, name, source in rows:
            slug = (slug or "").strip().strip("/").lower()
            if system not in SYSTEMS or not slug:
                counts["refused"] += 1
                continue
            row = index.get((system, slug))
            if row is None:
                fresh = Company(system=system, slug=slug, name=name or "",
                                sources=[source] if source else []).model_dump()
                data["companies"].append(fresh)
                index[(system, slug)] = fresh
                counts["new"] += 1
                continue
            counts["known"] += 1
            if source and source not in row["sources"]:
                row["sources"].append(source)
            if name and not row.get("name"):
                row["name"] = name
        _write(data)
    return counts


def update(company_id: str, **fields) -> Optional[Company]:
    with _lock:
        data = _read()
        for row in data["companies"]:
            if f"{row['system']}:{row['slug']}" == company_id:
                row.update(fields)
                _write(data)
                return Company(**row)
    return None


def record_harvest(source: str, links: int, found: dict, counts: dict) -> None:
    """What one list gave, kept so a source that stops working is visible."""
    with _lock:
        data = _read()
        data["harvests"] = [h for h in data["harvests"] if h["source"] != source]
        data["harvests"].append({"source": source, "at": utcnow(), "links": links,
                                 "found": found, **counts})
        _write(data)


def harvests() -> list[dict]:
    with _lock:
        return list(_read()["harvests"])


def stats() -> dict:
    rows = companies(include_dead=True)
    live = [c for c in rows if not c.dead]
    return {
        "companies": len(live),
        "dead": sum(1 for c in rows if c.dead),
        "unchecked": sum(1 for c in live if c.ok_at is None and c.error is None),
        "broken": sum(1 for c in live if c.error),
        "by_system": {s: sum(1 for c in live if c.system == s) for s in SYSTEMS},
        "roles": sum(c.roles for c in live),
        "at_level": sum(c.at_level for c in live),
    }
