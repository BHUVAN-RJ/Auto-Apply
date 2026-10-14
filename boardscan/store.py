"""Collected roles, in SQLite at `data/boardscan.db`.

The registry is a JSON file because two thousand short rows are nothing to
re-read. The roles are not: a full crawl is around ninety thousand open
roles, each with its description, and the page wants a page of them
filtered and sorted. That is a database, and `sqlite3` is in the standard
library, so it costs no dependency.

Two rules hold the whole design:

* **A role is identified by the board's own id**, not by its text, so the
  same role read every morning is one row and the diff that produces
  "today's new postings" is an insert that did not happen.
* **An insert never overwrites a decision.** `INSERT OR IGNORE` is what
  keeps a crawl from resetting `state`, `reason` and `found_at` on a role
  the person has already judged - the decline pile is the thing this
  service is being calibrated on, and a crawl that quietly cleared it
  would destroy the only data that matters.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Iterable, Optional

import paths
from boardscan import Role
from server.models import utcnow

ENV = "AUTOPILOT_BOARDSCAN_DB"

_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS roles (
    id            TEXT PRIMARY KEY,
    system        TEXT NOT NULL,
    slug          TEXT NOT NULL,
    board_id      TEXT NOT NULL,
    company       TEXT NOT NULL DEFAULT '',
    title         TEXT NOT NULL DEFAULT '',
    url           TEXT NOT NULL DEFAULT '',
    location      TEXT NOT NULL DEFAULT '',
    posted        TEXT NOT NULL DEFAULT '',
    text          TEXT NOT NULL DEFAULT '',
    found_at      TEXT NOT NULL,
    verdict       TEXT NOT NULL DEFAULT '',
    flags         TEXT NOT NULL DEFAULT '[]',
    seen_verdict  TEXT NOT NULL DEFAULT '',
    seen_job      TEXT,
    state         TEXT NOT NULL DEFAULT 'new',
    reason        TEXT NOT NULL DEFAULT '',
    decided_at    TEXT
);
CREATE INDEX IF NOT EXISTS roles_state   ON roles (state, found_at DESC);
CREATE INDEX IF NOT EXISTS roles_board   ON roles (system, slug);
CREATE INDEX IF NOT EXISTS roles_posted  ON roles (posted DESC);
CREATE INDEX IF NOT EXISTS roles_verdict ON roles (verdict);
"""

_FIELDS = ("id", "system", "slug", "board_id", "company", "title", "url", "location",
           "posted", "text", "found_at", "verdict", "flags", "seen_verdict", "seen_job",
           "state", "reason", "decided_at")


def path() -> Path:
    """Resolved per call: a module-level default would freeze a test's
    temporary directory out of existence (and has, twice, elsewhere in this
    repository)."""
    return Path(os.environ.get(ENV) or paths.DATA / "boardscan.db")


def connect() -> sqlite3.Connection:
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def _row_to_role(row: sqlite3.Row) -> Role:
    data = {k: row[k] for k in row.keys() if k != "id"}
    data["flags"] = json.loads(data.get("flags") or "[]")
    return Role(**data)


def add_new(roles: Iterable[Role]) -> list[Role]:
    """Insert the roles not already on file; return the ones that were new.

    That return value is the day's news, and it is the reason this is an
    insert rather than an upsert.
    """
    fresh: list[Role] = []
    with _lock, connect() as conn:
        for role in roles:
            values = (role.id, role.system, role.slug, role.board_id, role.company,
                      role.title, role.url, role.location, role.posted, role.text,
                      role.found_at, role.verdict, json.dumps(role.flags),
                      role.seen_verdict, role.seen_job, role.state, role.reason,
                      role.decided_at)
            cur = conn.execute(
                f"INSERT OR IGNORE INTO roles ({','.join(_FIELDS)}) "
                f"VALUES ({','.join('?' * len(_FIELDS))})", values)
            if cur.rowcount:
                fresh.append(role)
    return fresh


def known_ids(system: str, slug: str) -> set[str]:
    with _lock, connect() as conn:
        return {r["board_id"] for r in conn.execute(
            "SELECT board_id FROM roles WHERE system = ? AND slug = ?", (system, slug))}


def get(role_id: str) -> Optional[Role]:
    with _lock, connect() as conn:
        row = conn.execute("SELECT * FROM roles WHERE id = ?", (role_id,)).fetchone()
    return _row_to_role(row) if row else None


def listing(state: str = "", verdict: str = "", system: str = "", query: str = "",
            hide_seen: bool = False, limit: int = 200, offset: int = 0,
            with_text: bool = False) -> list[Role]:
    """The left-hand list: newest first, filtered the way the page asks.

    `with_text` is off by default because a page of two hundred roles with
    their descriptions is megabytes the list does not display.
    """
    where, args = [], []
    if state:
        where.append("state = ?")
        args.append(state)
    if verdict:
        where.append("verdict = ?")
        args.append(verdict)
    if system:
        where.append("system = ?")
        args.append(system)
    if hide_seen:
        where.append("seen_verdict = ''")
    if query:
        where.append("(company LIKE ? OR title LIKE ? OR location LIKE ?)")
        args += [f"%{query}%"] * 3
    columns = "*" if with_text else ",".join(f for f in _FIELDS if f != "text")
    sql = f"SELECT {columns} FROM roles"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY COALESCE(NULLIF(posted,''), found_at) DESC, found_at DESC LIMIT ? OFFSET ?"
    with _lock, connect() as conn:
        rows = conn.execute(sql, (*args, limit, offset)).fetchall()
    out = []
    for row in rows:
        data = {k: row[k] for k in row.keys() if k != "id"}
        data.setdefault("text", "")
        data["flags"] = json.loads(data.get("flags") or "[]")
        out.append(Role(**data))
    return out


def decide(role_id: str, state: str, reason: str = "") -> Optional[Role]:
    """The person's verdict on one role. `added` when it went into
    autopilot, `declined` with the reason when it did not - and the reason
    is the point, since the decline pile is what the filters are tuned
    from."""
    if state not in ("new", "added", "declined"):
        raise ValueError(f"not a state: {state}")
    with _lock, connect() as conn:
        conn.execute("UPDATE roles SET state = ?, reason = ?, decided_at = ? WHERE id = ?",
                     (state, reason, None if state == "new" else utcnow(), role_id))
    return get(role_id)


def mark_seen(role_id: str, verdict: str, job_id: Optional[str]) -> None:
    """What the identity check said about this role, cached on the row so
    the list can grey it out without asking again."""
    with _lock, connect() as conn:
        conn.execute("UPDATE roles SET seen_verdict = ?, seen_job = ? WHERE id = ?",
                     (verdict, job_id, role_id))


def counts() -> dict:
    with _lock, connect() as conn:
        total = conn.execute("SELECT COUNT(*) FROM roles").fetchone()[0]
        by_state = {r["state"]: r["n"] for r in conn.execute(
            "SELECT state, COUNT(*) AS n FROM roles GROUP BY state")}
        by_verdict = {r["verdict"] or "none": r["n"] for r in conn.execute(
            "SELECT verdict, COUNT(*) AS n FROM roles GROUP BY verdict")}
        seen = conn.execute("SELECT COUNT(*) FROM roles WHERE seen_verdict != ''").fetchone()[0]
        today = conn.execute("SELECT COUNT(*) FROM roles WHERE found_at >= ?",
                             (utcnow()[:10],)).fetchone()[0]
    return {"roles": total, "by_state": by_state, "by_verdict": by_verdict,
            "already_known": seen, "found_today": today}


def declines(limit: int = 500) -> list[dict]:
    """The calibration pile: what was declined, with the reason, newest
    first. Read by hand after the first week to decide which filters to
    tighten."""
    with _lock, connect() as conn:
        rows = conn.execute(
            "SELECT company, title, location, verdict, flags, reason, decided_at, url "
            "FROM roles WHERE state = 'declined' ORDER BY decided_at DESC LIMIT ?",
            (limit,)).fetchall()
    return [{**{k: r[k] for k in r.keys()}, "flags": json.loads(r["flags"] or "[]")} for r in rows]
