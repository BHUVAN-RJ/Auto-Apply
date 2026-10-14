"""The index itself: one SQLite file beside the data, never inside it.

The file is derived and disposable - delete it and `backfill()` builds it
again from the queue and the applications folder. Nothing in here writes to
anything this project's pipeline owns.

Every path is resolved inside a function. A module-level `Path` default
freezes at import, so a test that points `AUTOPILOT_HOME` somewhere else
would still write to the person's real data directory; that mistake has
been made twice in this repository.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator, Optional

import paths

from .sources import KnownJob

SCHEMA_VERSION = "1"

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    row_id       TEXT PRIMARY KEY,
    key          TEXT NOT NULL,
    display_id   TEXT NOT NULL,
    url          TEXT NOT NULL,
    canonical    TEXT NOT NULL,
    title        TEXT,
    company      TEXT,
    location     TEXT,
    status       TEXT,
    added_at     TEXT,
    decided_at   TEXT,
    folder       TEXT,
    origin       TEXT,
    reject       TEXT,
    title_norm   TEXT,
    company_norm TEXT,
    location_norm TEXT,
    simhash      TEXT,
    text_fp      TEXT
);
CREATE INDEX IF NOT EXISTS jobs_key ON jobs(key);
CREATE INDEX IF NOT EXISTS jobs_display ON jobs(display_id);
CREATE INDEX IF NOT EXISTS jobs_company ON jobs(company_norm);

CREATE TABLE IF NOT EXISTS job_keys (
    kind   TEXT NOT NULL,
    value  TEXT NOT NULL,
    row_id TEXT NOT NULL,
    PRIMARY KEY (kind, value, row_id)
);
CREATE INDEX IF NOT EXISTS job_keys_lookup ON job_keys(kind, value);

CREATE TABLE IF NOT EXISTS sources (
    name        TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    checked_at  TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    name  TEXT PRIMARY KEY,
    value TEXT
);
"""


def db_path() -> Path:
    """`data/identity.sqlite3`, or wherever `AUTOPILOT_IDENTITY_DB` says."""
    override = os.environ.get("AUTOPILOT_IDENTITY_DB")
    return Path(override) if override else paths.DATA / "identity.sqlite3"


@contextmanager
def connect(path: Optional[Path] = None) -> Iterator[sqlite3.Connection]:
    target = path or db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(target)
    connection.row_factory = sqlite3.Row
    try:
        connection.executescript(SCHEMA)
        connection.execute("INSERT OR IGNORE INTO meta (name, value) VALUES ('schema', ?)",
                           (SCHEMA_VERSION,))
        yield connection
        connection.commit()
    finally:
        connection.close()


def put(connection: sqlite3.Connection, job: KnownJob) -> None:
    """Add or replace one job and its keys.

    The simhash is stored as text: it is 64 unsigned bits and SQLite's
    INTEGER is signed, so half of them would come back negative.
    """
    from . import keys as keymod

    company = keymod.norm_company(job.company)
    connection.execute(
        """INSERT OR REPLACE INTO jobs (row_id, key, display_id, url, canonical, title,
               company, location, status, added_at, decided_at, folder, origin, reject,
               title_norm, company_norm, location_norm, simhash, text_fp)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (job.row_id, job.key, job.display_id, job.url, job.canonical, job.title,
         job.company, job.location, job.status, job.added_at, job.decided_at,
         job.folder, job.origin, job.reject,
         keymod.norm_title(job.title, job.company), company,
         keymod.norm_location(job.location), str(job.simhash), job.text_fp),
    )
    connection.execute("DELETE FROM job_keys WHERE row_id = ?", (job.row_id,))
    connection.executemany(
        "INSERT OR IGNORE INTO job_keys (kind, value, row_id) VALUES (?,?,?)",
        [(kind, value, job.row_id) for kind, value in job.identity_keys() if value],
    )


def keep_only(connection: sqlite3.Connection, row_ids: Iterable[str]) -> int:
    """Drop the rows a rebuild did not see again. Returns how many went."""
    wanted = set(row_ids)
    present = {row["row_id"] for row in connection.execute("SELECT row_id FROM jobs")}
    gone = present - wanted
    connection.executemany("DELETE FROM jobs WHERE row_id = ?", [(r,) for r in gone])
    connection.executemany("DELETE FROM job_keys WHERE row_id = ?", [(r,) for r in gone])
    return len(gone)


def by_key(connection: sqlite3.Connection, kind: str, value: str) -> list[sqlite3.Row]:
    if not value:
        return []
    return list(connection.execute(
        """SELECT jobs.* FROM job_keys JOIN jobs USING (row_id)
           WHERE job_keys.kind = ? AND job_keys.value = ?""", (kind, value)))


def by_company(connection: sqlite3.Connection, company_norm: str) -> list[sqlite3.Row]:
    if not company_norm:
        return []
    return list(connection.execute("SELECT * FROM jobs WHERE company_norm = ?", (company_norm,)))


def row(connection: sqlite3.Connection, row_id: str) -> Optional[sqlite3.Row]:
    found = connection.execute("SELECT * FROM jobs WHERE row_id = ?", (row_id,)).fetchone()
    return found


def cached_simhashes(connection: sqlite3.Connection) -> dict[str, tuple[int, str]]:
    """folder -> (simhash, posting.md fingerprint), so a refresh only hashes
    the postings whose file actually changed."""
    found: dict[str, tuple[int, str]] = {}
    for item in connection.execute(
            "SELECT folder, simhash, text_fp FROM jobs WHERE folder != '' AND simhash != '0'"):
        try:
            found[item["folder"]] = (int(item["simhash"]), item["text_fp"] or "")
        except (TypeError, ValueError):
            continue
    return found


def source_state(connection: sqlite3.Connection) -> dict[str, str]:
    return {item["name"]: item["fingerprint"]
            for item in connection.execute("SELECT name, fingerprint FROM sources")}


def set_source_state(connection: sqlite3.Connection, prints: dict[str, str], when: str) -> None:
    connection.executemany(
        "INSERT OR REPLACE INTO sources (name, fingerprint, checked_at) VALUES (?,?,?)",
        [(name, value, when) for name, value in prints.items()])


def counts(connection: sqlite3.Connection) -> dict[str, int]:
    jobs = connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    keys = connection.execute("SELECT COUNT(*) FROM job_keys").fetchone()[0]
    by_status = {item["status"] or "unknown": item["n"] for item in connection.execute(
        "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")}
    with_text = connection.execute("SELECT COUNT(*) FROM jobs WHERE simhash != '0'").fetchone()[0]
    return {"jobs": jobs, "keys": keys, "with_posting_text": with_text, **{f"status_{k}": v for k, v in by_status.items()}}
