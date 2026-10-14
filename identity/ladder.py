"""The ladder: four rules, cheapest and most certain first, and a verdict.

Every rule answers with its own name and the two values it compared, so a
verdict can be read and argued with rather than believed. No model is
consulted at any level, and no level looks at the network: the same probe
against the same index always produces the same answer.

    1  exact URL          certain   the same page, tracking stripped out
    2  board identity     certain   the same (board, org, job id)
    3  company and title  certain   when the place matches too, else likely
    4  resemblance        likely    same employer, near-identical title or
                                    a posting within a few bits of ours

Level 3 and 4 are fenced by `keys.conflicting`: two openings at one
employer that differ by level number (`Engineer I` against `Engineer II`),
or by being an internship rather than a job, are never called one job
however alike their words are. That is the dangerous false positive, and
it is the one this corpus actually contains.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from tailor import quality

from . import keys, store
from .sources import FURTHEST_FIRST

CERTAIN = "certain"
LIKELY = "likely"

BLOCK = "block"
WARN = "warn"
CLEAR = "clear"

# Already applied, or the form is on the screen right now.
APPLIED = {"submitted", "filled", "filling"}
# In autopilot and on its way: re-adding it duplicates the work and ends in
# two tailored resumes for one job. The owner's question is "have I applied
# to this, or already got this in the pipeline?", so both answers are BLOCK.
IN_PIPELINE = {"queued", "tailoring", "awaiting_review", "approved"}
# Decided against, or stopped. Worth another look: one row on the failed
# shelf stopped on an OpenRouter billing error, which says nothing about
# the job. Never re-added by accident, though, so it is a WARN.
DECIDED = {"skipped", "failed"}

# A rejection stops matching by resemblance after this long: the same title
# at the same company a season later is a new opening, not the one that was
# turned down. The rule, and the number, are `server.seen.REJECT_TTL_DAYS`.
REJECT_TTL_DAYS = 90

# Token-set Jaccard of two normalised titles, and the measurement that
# chose it. Over the 301 jobs on disk there are 50 pairs that share a
# usable normalised company and are not already certain by URL or board
# id. Read by hand, in descending order of similarity: 1.00 ("Associate
# Engineer - AI and Agentic" against "AI and Agentic Engineer Associate",
# one job, two spellings), then 0.80 ("Forward Deployed Software Engineer"
# against "Forward Deployed Software Engineer, New Grad - Commercial", two
# real Palantir openings), then 0.75 ("Software Engineer I" against
# "Software Engineer I, Discovery" at Twitch, two openings). So the only
# threshold the corpus supports is above 0.80, and 0.85 is it.
#
# What that costs: SingleStore's two "Helios" new-grad postings score 0.75
# and are probably one role reposted. A missed resemblance is a WARN not
# raised; a wrong one is a WARN on a job the owner has not applied to, and
# a service that cries wolf is one nobody reads.
TITLE_SIMILARITY = 0.85

# Hamming distance between two 64-bit simhashes of the posting text
# (`tailor.quality.simhash`: word trigrams over `quality.clean`). 6 is the
# number `server.seen` already uses and this corpus does not contradict
# it - but only once `keys.usable_posting` has thrown out the text that is
# not a description. Before that filter, six pairs sat at 0 or 1 bits and
# every one of them was two unrelated jobs whose captures were the same
# sign-in wall, the same bare form or the same whole board.
TEXT_DISTANCE = 6


@dataclass
class Evidence:
    """One thing a rule compared, in the words the rule compared them in."""

    compared: str
    mine: str
    theirs: str
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Match:
    level: int
    rule: str
    confidence: str
    key: str
    display_id: str
    url: str
    company: str
    title: str
    status: str
    decided_at: str
    folder: str
    evidence: list[Evidence] = field(default_factory=list)

    @property
    def rank(self) -> tuple:
        try:
            status = FURTHEST_FIRST.index(self.status)
        except ValueError:
            status = len(FURTHEST_FIRST)
        return (0 if self.confidence == CERTAIN else 1, self.level, status,
                -_seconds(self.decided_at))

    def to_dict(self) -> dict:
        data = asdict(self)
        data["evidence"] = [e.to_dict() for e in self.evidence]
        return data


@dataclass
class Verdict:
    verdict: str
    reason: str
    matches: list[Match] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)
    searched: int = 0

    @property
    def best(self) -> Optional[Match]:
        return self.matches[0] if self.matches else None

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "reason": self.reason,
            "stale": self.stale,
            "searched": self.searched,
            "matches": [m.to_dict() for m in self.matches],
        }


def _seconds(when: str) -> float:
    """A timestamp as a number, so the newer of two matches sorts first."""
    try:
        moment = datetime.fromisoformat(when)
    except (TypeError, ValueError):
        return 0.0
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


@dataclass
class Probe:
    """The posting being asked about, with its keys worked out once."""

    url: str
    title: str = ""
    company: str = ""
    location: str = ""
    text: str = ""

    def __post_init__(self) -> None:
        self.canonical = keys.canonical(self.url)
        self.ats = keys.ats_identity(self.url)
        self.jobright = keys.jobright_id(self.url)
        if not self.company:
            self.company = keys.company_from_title(self.title)
        self.company_norm = keys.norm_company(self.company)
        self.title_norm = keys.norm_title(self.title, self.company)
        self.location_norm = keys.norm_location(self.location)
        self.simhash = (quality.simhash(self.text)
                        if keys.usable_posting(self.text) else 0)


def expired(status: str, decided_at: str, now: Optional[datetime] = None) -> bool:
    """A rejection old enough to stop matching by resemblance."""
    if status != "skipped":
        return False
    try:
        then = datetime.fromisoformat(decided_at)
    except (TypeError, ValueError):
        return False
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (now or datetime.now(timezone.utc)) - then > timedelta(days=REJECT_TTL_DAYS)


def _match(row, level: int, rule: str, confidence: str, evidence: list[Evidence]) -> Match:
    return Match(level=level, rule=rule, confidence=confidence, key=row["key"],
                 display_id=row["display_id"], url=row["url"],
                 company=row["company"] or "", title=row["title"] or "",
                 status=row["status"] or "", decided_at=row["decided_at"] or "",
                 folder=row["folder"] or "", evidence=evidence)


def matches(connection, probe: Probe, now: Optional[datetime] = None) -> list[Match]:
    """Every job in the index this posting could be, best first."""
    found: dict[str, Match] = {}

    def offer(match: Match) -> None:
        kept = found.get(match.key + match.display_id)
        if kept is None or match.rank < kept.rank:
            found[match.key + match.display_id] = match

    for row in store.by_key(connection, "url", probe.canonical):
        offer(_match(row, 1, "exact url", CERTAIN, [
            Evidence("canonical url", probe.canonical, row["canonical"])]))

    if probe.ats:
        value = ":".join(probe.ats)
        for row in store.by_key(connection, "ats", value):
            offer(_match(row, 2, "same job at the same board", CERTAIN, [
                Evidence("board, org, job id", value, value),
                Evidence("url", probe.url, row["url"], "two shapes of one board link")]))

    if probe.jobright:
        for row in store.by_key(connection, "jobright", probe.jobright):
            offer(_match(row, 2, "same Jobright posting", CERTAIN, [
                Evidence("jobright posting id", probe.jobright, probe.jobright)]))

    if probe.company_norm and probe.title_norm:
        value = f"{probe.company_norm}|{probe.title_norm}"
        for row in store.by_key(connection, "pair", value):
            if expired(row["status"] or "", row["decided_at"] or "", now):
                continue
            theirs = row["location_norm"] or ""
            same_place = bool(probe.location_norm) and probe.location_norm == theirs
            evidence = [
                Evidence("company", probe.company_norm, row["company_norm"] or ""),
                Evidence("title", probe.title_norm, row["title_norm"] or ""),
                Evidence("location", probe.location_norm, theirs,
                         "" if same_place else "not compared: one side has no place"
                         if not probe.location_norm or not theirs else "different places"),
            ]
            if probe.location_norm and theirs and not same_place:
                # Same role, same employer, another city: a real second
                # opening often enough to be worth a look, never a block.
                offer(_match(row, 3, "same company and title, different place", LIKELY, evidence))
                continue
            offer(_match(row, 3,
                         "same company, title and place" if same_place else "same company and title",
                         CERTAIN if same_place else LIKELY, evidence))

    if probe.company_norm:
        for row in store.by_company(connection, probe.company_norm):
            if (row["key"] + row["display_id"]) in found:
                continue
            if expired(row["status"] or "", row["decided_at"] or "", now):
                continue
            theirs_title = row["title"] or ""
            if keys.conflicting(probe.title, theirs_title, probe.company, row["company"] or ""):
                continue
            score = keys.title_similarity(probe.title, theirs_title,
                                          probe.company, row["company"] or "")
            if probe.title_norm and score >= TITLE_SIMILARITY:
                offer(_match(row, 4, "same company, near-identical title", LIKELY, [
                    Evidence("company", probe.company_norm, row["company_norm"] or ""),
                    Evidence("title", probe.title_norm, row["title_norm"] or "",
                             f"token-set similarity {score:.2f} >= {TITLE_SIMILARITY}")]))
                continue
            if not probe.simhash:
                continue
            try:
                theirs = int(row["simhash"] or 0)
            except (TypeError, ValueError):
                continue
            if not theirs:
                continue
            distance = quality.distance(probe.simhash, theirs)
            if distance <= TEXT_DISTANCE:
                offer(_match(row, 4, "same company, same description", LIKELY, [
                    Evidence("company", probe.company_norm, row["company_norm"] or ""),
                    Evidence("posting simhash", str(probe.simhash), str(theirs),
                             f"{distance} bits apart, {TEXT_DISTANCE} allowed")]))

    return sorted(found.values(), key=lambda m: m.rank)


def verdict(found: list[Match], stale: Optional[list[str]] = None, searched: int = 0) -> Verdict:
    """BLOCK, WARN or CLEAR, and the sentence that says why.

    BLOCK is a certain match on a job already applied for or already in the
    pipeline. WARN is a certain match on a job that was rejected or
    stopped - which the owner may legitimately want another crack at - or
    any match that is only likely. CLEAR is nothing at all.
    """
    stale = stale or []
    if not found:
        return Verdict(CLEAR, "No job in the index matches this posting.",
                       [], stale, searched)
    certain = [m for m in found if m.confidence == CERTAIN]
    blocking = [m for m in certain if m.status in APPLIED or m.status in IN_PIPELINE]
    if blocking:
        top = blocking[0]
        what = ("already applied for" if top.status in APPLIED
                else "already in autopilot")
        return Verdict(BLOCK,
                       f"{what}: {top.title or 'this role'} at {top.company or 'this company'} "
                       f"({top.display_id}, {top.status} {top.decided_at or 'at an unknown time'}) "
                       f"- matched by {top.rule}.",
                       found, stale, searched)
    if certain:
        top = certain[0]
        return Verdict(WARN,
                       f"the same posting was {top.status}: {top.title or 'this role'} at "
                       f"{top.company or 'this company'} ({top.display_id}, "
                       f"{top.decided_at or 'at an unknown time'}) - matched by {top.rule}.",
                       found, stale, searched)
    top = found[0]
    return Verdict(WARN,
                   f"this may be {top.title or 'a role'} at {top.company or 'this company'} "
                   f"({top.display_id}, {top.status}) - matched by {top.rule}, which is a "
                   f"resemblance and not a proof.",
                   found, stale, searched)
