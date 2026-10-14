"""Building and keeping the index, and knowing when it is behind the data.

The queue changes under this package all day: a job is captured, tailored,
filled, submitted. An index that answers from what it knew yesterday can
answer CLEAR about a job that was submitted this morning, which is the one
failure this service exists to prevent - so every lookup checks the sources'
fingerprints first and refreshes before it answers.

The check is two `stat` calls and one directory scan; the refresh re-reads
the queue and every application folder's header (about 0.3 s over 380
folders) and re-hashes only the postings whose file changed. A full build
from nothing, hashing every posting, is about 5 s.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from tailor import quality

from . import keys, store
from .sources import KnownJob, collect, fingerprints, posting_text


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def build(connection, cache: Optional[dict[str, tuple[int, str]]] = None) -> dict:
    """Read every source and replace the index with what they say.

    Idempotent: running it twice over unchanged data leaves the same rows.
    `cache` maps a folder to the (simhash, fingerprint) already stored, so
    an unchanged posting is not hashed again.
    """
    cache = cache if cache is not None else {}
    hashed = 0
    reused = 0

    def simhash_for(folder: Path, fingerprint: str) -> tuple[int, str]:
        nonlocal hashed, reused
        known = cache.get(str(folder))
        if known and known[1] == fingerprint and known[0]:
            reused += 1
            return known
        text = posting_text(folder)
        if not keys.usable_posting(text):
            # A sign-in wall, a bare form or a whole board read end to
            # end. Hashing it would say two unrelated jobs are the same
            # posting, which is exactly what it said before this check.
            return 0, fingerprint
        hashed += 1
        return quality.simhash(text), fingerprint

    jobs = collect(simhash_for)
    for job in jobs:
        store.put(connection, job)
    dropped = store.keep_only(connection, [job.row_id for job in jobs])
    store.set_source_state(connection, fingerprints(), _now())
    return {"jobs": len(jobs), "hashed": hashed, "reused": reused, "dropped": dropped}


def backfill() -> dict:
    """Build the index from scratch, from the queue and `applications/`."""
    with store.connect() as connection:
        report = build(connection, store.cached_simhashes(connection))
        report.update(store.counts(connection))
        report["db"] = str(store.db_path())
        return report


def stale(connection) -> list[str]:
    """Which sources have moved since the index was built."""
    stored = store.source_state(connection)
    now = fingerprints()
    return sorted(name for name, value in now.items() if stored.get(name) != value)


def ensure_fresh(connection, refresh: bool = True) -> list[str]:
    """Catch the index up, and say what was behind.

    Returns the sources that were stale. With `refresh` false it only
    reports, which is how a caller that cannot afford the read can still
    be told its answer is not to be trusted.
    """
    behind = stale(connection)
    if behind and refresh:
        build(connection, store.cached_simhashes(connection))
    return behind


def add(job) -> str:
    """Index one job now, without reading everything again.

    Takes a `server.models.Job` (or anything with `url`, `title`,
    `company`, `status`, `added_at` and `app_dir`). The source fingerprints
    are deliberately *not* stamped: the queue file changed for this row, and
    it may have changed for others too, so the next lookup still refreshes.
    """
    url = getattr(job, "url", "") or ""
    status = getattr(job, "status", "")
    folder = getattr(job, "app_dir", "") or ""
    known = KnownJob(
        key=keys.identity_key(url),
        display_id=getattr(job, "id", "") or "",
        url=url,
        canonical=keys.canonical(url),
        title=getattr(job, "title", "") or "",
        company=getattr(job, "company", "") or "",
        status=getattr(status, "value", status) or "",
        added_at=getattr(job, "added_at", "") or "",
        folder=folder,
        origin="queue",
    )
    known.decided_at = known.added_at
    if folder and Path(folder).is_dir():
        text = posting_text(Path(folder))
        if keys.usable_posting(text):
            known.simhash = quality.simhash(text)
    with store.connect() as connection:
        store.put(connection, known)
    return known.row_id


def collisions() -> dict[str, list[dict]]:
    """Short display ids that name more than one job.

    `server.models.url_hash` keeps four hex characters of the URL, so there
    are 65,536 of them; two Twitch postings collided at 270 rows and one of
    the two became unreachable from the review page (KNOWN_ISSUES #1). This
    reports the collisions in the data as it stands, keyed by the short id,
    with the long key each job would have instead.
    """
    found: dict[str, list[dict]] = {}
    with store.connect() as connection:
        ensure_fresh(connection)
        rows = list(connection.execute(
            "SELECT display_id, key, url, title, company, status, folder FROM jobs"))
    by_display: dict[str, list] = {}
    for item in rows:
        by_display.setdefault(item["display_id"], []).append(item)
    for display_id, group in by_display.items():
        if len({item["key"] for item in group}) > 1:
            found[display_id] = [dict(item) for item in group]
    return found


def stats() -> dict:
    with store.connect() as connection:
        behind = stale(connection)
        report = store.counts(connection)
        report["db"] = str(store.db_path())
        report["stale"] = behind
        report["sources"] = store.source_state(connection)
        return report
