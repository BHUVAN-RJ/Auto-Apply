"""Have I already applied to this job?

A deterministic answer, with the rule that decided it and the two values
that rule compared. No model is used at any point - not for normalising a
title, not for matching, not as a tiebreaker - so the same posting always
gets the same verdict and a wrong one can be read and corrected rather
than re-rolled.

    from identity import lookup
    verdict = lookup("https://job-boards.greenhouse.io/figma/jobs/123",
                     title="Software Engineer I", company="Figma",
                     location="San Francisco, CA", text=posting_text)
    verdict.verdict   # "block", "warn" or "clear"
    verdict.reason    # the sentence a person reads
    verdict.matches   # every job it could be, best first, with evidence

Three verdicts:

* **block** - a certain match on a job already applied for (submitted,
  filled, filling) or already in the pipeline. Do not apply again.
* **warn** - a certain match on a job that was rejected or that stopped, or
  any match that is only a resemblance. The reason says which.
* **clear** - nothing in the index matches.

The index is SQLite, under `paths.DATA`, built from `data/queue.json` and
`applications/**` and never writing to either. It is checked against those
sources' fingerprints on every lookup and refreshed when they have moved,
because a stale CLEAR is exactly the failure this service exists to
prevent; `Verdict.stale` names any source that was still behind.

This supersedes `server/seen.py`, which answers the same question by
scanning the queue on every call and is left untouched. The differences
worth knowing about:

* the keys are indexed, so a lookup is a few indexed reads;
* tracking parameters are dropped by a deny-list rather than job
  parameters kept by an allow-list, which is what lets two Eightfold jobs
  at one tenant stay two jobs (`seen.KEEP_PARAMS` has no `pid`);
* titles are compared by token set only. `seen` takes the greater of a
  token set and a character ratio, and "Software Engineer I" against
  "Software Engineer II" is 0.95 alike as characters;
* `identity_key(url)` is 16 hex characters of the canonical URL, where
  `server.models.url_hash` is four hex characters of the raw one - the
  collision in KNOWN_ISSUES #1.
"""

from __future__ import annotations

import hashlib

from . import index, keys, ladder, store
from .index import add, backfill, collisions, stats
from .keys import canonical, identity_key
from .ladder import BLOCK, CLEAR, CERTAIN, LIKELY, WARN, Match, Probe, Verdict

__all__ = [
    "lookup", "explain", "backfill", "add", "collisions", "stats",
    "canonical", "identity_key", "Verdict", "Match", "Probe",
    "BLOCK", "WARN", "CLEAR", "CERTAIN", "LIKELY",
]


def lookup(url: str, title: str = "", company: str = "", location: str = "",
           text: str = "", refresh: bool = True) -> Verdict:
    """The verdict for one posting.

    `title`, `company`, `location` and `text` are optional and only make
    the lower rungs of the ladder reachable: with a URL alone, levels 1 and
    2 still answer, and they are the certain ones.
    """
    probe = Probe(url=url, title=title, company=company, location=location, text=text)
    with store.connect() as connection:
        behind = index.ensure_fresh(connection, refresh=refresh)
        found = ladder.matches(connection, probe)
        searched = store.counts(connection)["jobs"]
    return ladder.verdict(found, behind, searched)


def explain(url: str, title: str = "", company: str = "", location: str = "",
            text: str = "") -> dict:
    """The verdict plus every key the posting was looked up by.

    For reading a wrong answer: it says what the URL canonicalised to, what
    board identity was read out of it, and what the company, title and
    place normalised to, which is where a wrong verdict almost always is.
    """
    probe = Probe(url=url, title=title, company=company, location=location, text=text)
    answer = lookup(url, title, company, location, text)
    return {
        "probe": {
            "url": url,
            "canonical": probe.canonical,
            "identity_key": identity_key(url),
            "display_id": hashlib.sha256(url.encode()).hexdigest()[:4],
            "ats": ":".join(probe.ats) if probe.ats else "",
            "jobright": probe.jobright,
            "company_norm": probe.company_norm,
            "title_norm": probe.title_norm,
            "location_norm": probe.location_norm,
            "simhash": str(probe.simhash),
        },
        "thresholds": {
            "title_similarity": ladder.TITLE_SIMILARITY,
            "text_distance": ladder.TEXT_DISTANCE,
            "reject_ttl_days": ladder.REJECT_TTL_DAYS,
        },
        "verdict": answer.to_dict(),
    }
