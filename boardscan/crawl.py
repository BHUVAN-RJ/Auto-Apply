"""The daily read: every company in the registry, its own board, in full.

One company is one request and every open role comes back with its
description, so the whole crawl is network-bound and embarrassingly
parallel. Measured on a sample of 75 boards (2026-10-14): 7.3 seconds at
12 workers, 3,570 open roles, 130 past the filters. The registry's 1,979
companies are therefore about three minutes.

Three steps, all code, no model anywhere:

1. **The title and the place**, `scout/filter.py` - `at_level` against the
   level words a watch uses, and `outside_us` to drop a role whose every
   listed place names another country. This is what turns 94,000 roles
   into a few thousand, and it is pure string work.
2. **The screen**, `tailor/screen.py` - the same twelve deterministic rules
   the posting banner runs, over the description the board already sent, so
   a role arrives with its flags on it and nothing is fetched twice. The
   verdict is advice: a `reject` is listed, greyed, with the rule that
   fired, because the person is calibrating these filters in week one and a
   rule that is wrong has to be visible to be fixed.
3. **Have I applied already**, `boardscan/known.py` - over autopilot's own
   data. A certain match on a job already submitted is never offered.

What the crawl never does is decide. It inserts roles the board had and we
did not, and `INSERT OR IGNORE` means a role the person has already judged
keeps its judgement.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

import httpx

from boardscan import (DEFAULT_WORKERS, LEVEL_NEGATIVE, LEVEL_POSITIVE, Company, Role,
                    known, registry, store)
from scout import Posting, Watch, filter as level_filter
from scout import providers
from server.models import utcnow
from tailor import screen as screener

READERS: dict[str, Callable[[Watch, httpx.Client], list[Posting]]] = {
    "greenhouse": providers.greenhouse,
    "lever": providers.lever,
    "ashby": providers.ashby,
}

# The description kept on the row. The board sends the whole posting and
# the screen has already read it; what the page needs is enough to read by
# eye, and 90,000 whole postings is a database nobody needs.
TEXT_KEEP = 20_000


class BoardGone(RuntimeError):
    """The board answered 404: the company has left that system, or the slug
    was a bad guess. Recorded as `dead`, never retried, never deleted."""


def read_board(company: Company, http: httpx.Client) -> list[Posting]:
    watch = Watch(url=company.url, company=company.name or company.slug,
                  args={"slug": company.slug})
    try:
        return READERS[company.system](watch, http)
    except httpx.HTTPStatusError as e:
        if e.response.status_code in (404, 410):
            raise BoardGone(f"{e.response.status_code} from {company.system}") from e
        raise


def at_level(posting: Posting, positive: list[str], negative: list[str],
             us_only: bool = True) -> bool:
    """The cheap half of the filter: the title says the level and the place
    is not plainly another country."""
    if not level_filter.at_level(posting.title, positive, negative):
        return False
    if us_only and posting.location and level_filter.outside_us(posting.location):
        return False
    return True


def judge(posting: Posting, facts: str) -> tuple[str, list[str]]:
    """The deterministic screen's verdict and the rules that fired.

    A posting the board sent with no description cannot be screened, and
    says so rather than passing silently: `""` is "not screened", which the
    page shows differently from `ok`.
    """
    if not posting.text or not posting.text.strip():
        return "", []
    try:
        result = screener.screen(posting.text, title=posting.title, url=posting.url, facts=facts)
    except Exception as e:                      # noqa: BLE001 - a screen failure is not a crawl failure
        return "", [f"screen failed: {type(e).__name__}"]
    flags = [f"{f.category}/{f.severity}" for f in getattr(result, "flags", [])]
    return getattr(result, "verdict", "") or "", flags


def crawl_company(company: Company, facts: str = "", http: httpx.Client | None = None,
                  positive: Optional[list[str]] = None, negative: Optional[list[str]] = None,
                  check_known: bool = True) -> dict:
    """One board, read and folded in. Never raises: the failure is recorded
    on the company row and shown red, the way a broken Scout watch is."""
    own = http is None
    http = http or providers.client()
    positive = positive if positive is not None else list(LEVEL_POSITIVE)
    negative = negative if negative is not None else list(LEVEL_NEGATIVE)
    try:
        postings = read_board(company, http)
    except BoardGone as e:
        registry.update(company.id, dead=True, error=str(e), checked_at=utcnow())
        return {"company": company.id, "dead": True, "error": str(e), "roles": 0,
                "at_level": 0, "new": 0}
    except Exception as e:                      # noqa: BLE001
        error = f"{type(e).__name__}: {e}"[:200]
        registry.update(company.id, error=error, checked_at=utcnow())
        return {"company": company.id, "error": error, "roles": 0, "at_level": 0, "new": 0}
    finally:
        if own:
            http.close()

    matching = [p for p in postings if at_level(p, positive, negative)]
    rows: list[Role] = []
    for posting in matching:
        verdict, flags = judge(posting, facts)
        rows.append(Role(system=company.system, slug=company.slug, board_id=posting.id,
                         company=company.name or company.slug, title=posting.title,
                         url=posting.url or company.url, location=posting.location,
                         posted=posting.posted, text=posting.text[:TEXT_KEEP],
                         verdict=verdict, flags=flags))
    fresh = store.add_new(rows)
    if check_known:
        for role in fresh:
            answer = known.check(role.url, title=role.title, company=role.company, text=role.text)
            if answer.level:
                store.mark_seen(role.id, answer.level, answer.job_id)
                if answer.block:
                    store.decide(role.id, "declined", f"already applied ({answer.status})")
    registry.update(company.id, roles=len(postings), at_level=len(matching),
                    checked_at=utcnow(), ok_at=utcnow(), error=None, dead=False)
    return {"company": company.id, "roles": len(postings), "at_level": len(matching),
            "new": len(fresh)}


def crawl(systems: tuple[str, ...] = (), limit: int = 0, workers: int = DEFAULT_WORKERS,
          check_known: bool = True, only_unchecked: bool = False,
          progress: Optional[Callable[[dict], None]] = None) -> dict:
    """The whole registry, or a slice of it while trying something out.

    `only_unchecked` is the validation pass: a harvested slug is a guess
    until the board answers, so the first crawl after a harvest is also
    what marks the dead ones.
    """
    rows = registry.companies()
    if systems:
        rows = [c for c in rows if c.system in systems]
    if only_unchecked:
        rows = [c for c in rows if c.ok_at is None and not c.error]
    rows = [c for c in rows if c.enabled]
    if limit:
        rows = rows[:limit]

    facts = ""
    try:
        facts, _ = screener.load_facts()
    except Exception:                           # noqa: BLE001 - the screen has its own fallback
        pass

    started = time.time()
    out: list[dict] = []

    def one(company: Company) -> dict:
        http = providers.client()
        try:
            result = crawl_company(company, facts=facts, http=http, check_known=check_known)
        finally:
            http.close()
        if progress:
            progress(result)
        return result

    if rows:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            out = list(pool.map(one, rows))

    return {
        "companies": len(rows),
        "seconds": round(time.time() - started, 1),
        "roles": sum(r.get("roles", 0) for r in out),
        "at_level": sum(r.get("at_level", 0) for r in out),
        "new": sum(r.get("new", 0) for r in out),
        "dead": sum(1 for r in out if r.get("dead")),
        "broken": [r for r in out if r.get("error") and not r.get("dead")][:20],
        "errors": sum(1 for r in out if r.get("error")),
    }
