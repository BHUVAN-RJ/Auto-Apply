"""Watches and hits on disk: `data/scout.json`, one file, written whole
and atomically, read under a lock. Small enough that a page poll
re-reading it is nothing."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Optional

from scout import Hit, Watch

ROOT = Path(__file__).resolve().parent.parent
ENV = "AUTOPILOT_SCOUT_FILE"

_lock = threading.RLock()


def path() -> Path:
    return Path(os.environ.get(ENV, ROOT / "data" / "scout.json"))


def _read() -> dict:
    p = path()
    if not p.exists():
        return {"watches": [], "hits": []}
    try:
        data = json.loads(p.read_text() or "{}")
    except json.JSONDecodeError:
        return {"watches": [], "hits": []}
    data.setdefault("watches", [])
    data.setdefault("hits", [])
    return data


def _write(data: dict) -> None:
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, p)


def watches() -> list[Watch]:
    with _lock:
        return [Watch(**w) for w in _read()["watches"]]


def hits() -> list[Hit]:
    with _lock:
        return [Hit(**h) for h in _read()["hits"]]


def get_watch(watch_id: str) -> Optional[Watch]:
    return next((w for w in watches() if w.id == watch_id), None)


def get_hit(hit_id: str) -> Optional[Hit]:
    return next((h for h in hits() if h.id == hit_id), None)


def add_watch(watch: Watch) -> tuple[Watch, bool]:
    with _lock:
        data = _read()
        for raw in data["watches"]:
            if Watch(**raw).id == watch.id:
                return Watch(**raw), False
        data["watches"].append(watch.model_dump())
        _write(data)
        return watch, True


def update_watch(watch_id: str, **fields) -> Optional[Watch]:
    with _lock:
        data = _read()
        for i, raw in enumerate(data["watches"]):
            if Watch(**raw).id == watch_id:
                updated = Watch(**(raw | fields))
                data["watches"][i] = updated.model_dump()
                _write(data)
                return updated
        return None


def remove_watch(watch_id: str) -> bool:
    with _lock:
        data = _read()
        before = len(data["watches"])
        data["watches"] = [w for w in data["watches"] if Watch(**w).id != watch_id]
        data["hits"] = [h for h in data["hits"] if h["watch_id"] != watch_id]
        _write(data)
        return len(data["watches"]) < before


def add_hits(new: list[Hit]) -> list[Hit]:
    """Append the hits not already on file; returns the ones added."""
    with _lock:
        data = _read()
        known = {Hit(**h).id for h in data["hits"]}
        added = [h for h in new if h.id not in known]
        data["hits"].extend(h.model_dump() for h in added)
        _write(data)
        return added


def update_hit(hit_id: str, **fields) -> Optional[Hit]:
    with _lock:
        data = _read()
        for i, raw in enumerate(data["hits"]):
            if Hit(**raw).id == hit_id:
                updated = Hit(**(raw | fields))
                data["hits"][i] = updated.model_dump()
                _write(data)
                return updated
        return None
