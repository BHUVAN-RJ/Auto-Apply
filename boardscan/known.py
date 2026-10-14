"""Has this role already been applied for?

The answer must be deterministic and it must be right: the same posting
arrives from Jobright, from LinkedIn, from the Simplify list and from this
crawl, under four different URLs, and applying twice to one employer is the
failure the whole collection step is built to avoid.

There are two implementations of that question in this repository. The one
that has always been here is `server/seen.py`, which answers for the banner
on a posting page: canonical URL, `jr_id` and ATS job id for a certain
match, company plus title similarity or a posting simhash for a likely one.
The one being built alongside this package is `identity/`, an indexed
service over the same data with a wider ladder of rules.

This module asks for the better one and settles for the one that is there,
so the crawl does not have to wait for the index and does not have to be
rewritten when it lands. Both are read-only on autopilot's data.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

BLOCK_STATUS = ("submitted", "filled", "filling")


@dataclass
class Answer:
    """What the check decided, in the terms the list shows.

    `level` is `""` when nothing matched, `high` for a certain match and
    `confident` for a likely one - the words `server/seen.py` already uses,
    so the page's wording does not change when the backend does.
    """

    level: str = ""
    job_id: Optional[str] = None
    status: str = ""
    company: str = ""
    title: str = ""
    decided_at: str = ""
    rule: str = ""
    block: bool = False     # certain, and already applied for: never offer it again


def _by_identity(url: str, title: str, company: str, text: str) -> Optional[Answer]:
    """The indexed service, when it exists. Imported here rather than at
    module scope so this package runs before it does."""
    try:
        import identity                                   # noqa: PLC0415
    except Exception:                                     # noqa: BLE001
        return None
    lookup = getattr(identity, "lookup", None)
    if lookup is None:
        return None
    verdict = lookup(url, title=title or None, company=company or None, text=text or None)
    best = (getattr(verdict, "matches", None) or [None])[0]
    if best is None:
        return Answer(rule=getattr(verdict, "rule", "identity"))
    status = str(getattr(best, "status", "") or "")
    certain = str(getattr(best, "confidence", "")) == "certain"
    return Answer(
        level="high" if certain else "confident",
        job_id=getattr(best, "job_id", None),
        status=status,
        company=str(getattr(best, "company", "") or ""),
        title=str(getattr(best, "title", "") or ""),
        decided_at=str(getattr(best, "decided_at", "") or ""),
        rule=str(getattr(verdict, "rule", "") or "identity"),
        block=certain and status in BLOCK_STATUS,
    )


def _by_seen(url: str, title: str, company: str, text: str) -> Answer:
    """The banner's own check, which has been answering this question for
    every posting page since the beginning."""
    from server import seen                               # noqa: PLC0415

    match = seen.find(url, title=title, company=company, text=text)
    if match is None:
        return Answer(rule="seen")
    status = str(getattr(match, "status", "") or "")
    return Answer(level=str(getattr(match, "level", "") or ""), job_id=getattr(match, "job_id", None),
                  status=status, company=str(getattr(match, "company", "") or ""),
                  title=str(getattr(match, "title", "") or ""),
                  decided_at=str(getattr(match, "at", "") or ""), rule="seen",
                  block=getattr(match, "level", "") == "high" and status in BLOCK_STATUS)


def check(url: str, title: str = "", company: str = "", text: str = "") -> Answer:
    """One role against everything autopilot already holds.

    A failure answers "nothing matched" with the reason in `rule`, because
    the crawl must not stop on it - but note that this is the one place
    where a wrong "nothing matched" costs a duplicate application, so the
    page shows the reason rather than hiding it.
    """
    try:
        answer = _by_identity(url, title, company, text)
        if answer is not None:
            return answer
    except Exception as e:                                # noqa: BLE001
        return Answer(rule=f"identity failed: {type(e).__name__}")
    try:
        return _by_seen(url, title, company, text)
    except Exception as e:                                # noqa: BLE001
        return Answer(rule=f"seen failed: {type(e).__name__}")
