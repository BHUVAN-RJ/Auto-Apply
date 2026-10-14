"""Is this posting already in autopilot?

Asked by `/screen` the moment a job page opens and by `/capture` before a
row is added, so one job is not tailored, filled, or applied to twice. No
model: everything here is string work over the queue and the archive.

Two levels, both shown on the page, neither ever blocks the person:

- ``high``: the same posting, provably. Same canonical URL, same Jobright
  posting id, or the same job id at the same applicant-tracking system.
- ``confident``: the same employer and (the same role by title, or the same
  description by simhash). Good enough to stop the countdown and say so;
  wrong often enough to keep an "add anyway".

A rejection stops matching at the confident level after ``REJECT_TTL_DAYS``:
the same title at the same company a season later is a new opening.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlsplit

import identity
from identity import sources as identity_sources
from archive import store
from tailor import quality

from . import postings, queue
from .models import REJECT_LABELS, Job, Status

REJECT_TTL_DAYS = 90
TITLE_SIMILARITY = 0.8
TEXT_DISTANCE = 6

# Sites that list jobs rather than post them. A URL there is never a job.
SOURCE_HOSTS = ("jobright.ai",)

# One id per applicant-tracking system, wherever it is hosted. Two URLs
# with the same (system, id) are one posting.
ATS_IDS = [
    ("ashby", re.compile(r"ashbyhq\.com/[^/]+/([0-9a-f-]{36})")),
    ("greenhouse", re.compile(r"greenhouse\.io/[^/]+/jobs/(\d+)")),
    # The embedded board: one path for every job, the posting is the token.
    ("greenhouse", re.compile(r"greenhouse\.io/embed/job_app\?(?:[^#]*&)?token=(\d+)")),
    ("lever", re.compile(r"lever\.co/[^/]+/([0-9a-f-]{36})")),
    ("workday", re.compile(r"myworkdayjobs\.com/.*?/job/.*?_([A-Z0-9-]+)(?:[/?#]|$)")),
    ("oracle", re.compile(r"oraclecloud\.com/.*?/job/(\d+)")),
    ("icims", re.compile(r"icims\.com/jobs/(\d+)")),
    ("smartrecruiters", re.compile(r"smartrecruiters\.com/[^/]+/(\d+)")),
    ("workable", re.compile(r"workable\.com/(?:j|jobs)/([A-Za-z0-9]+)")),
    ("jobright", re.compile(r"jobright\.ai/jobs/info/([A-Za-z0-9_-]+)")),
]

# Query parameters worth keeping: they are the job, not the visitor.
# Greenhouse's embedded board (`/embed/job_app?for=<company>&token=<id>`)
# has one path for every job; dropping its query made every embed the
# same page and a new job "already in autopilot".
KEEP_PARAMS = {"gh_jid", "jobid", "job_id", "id", "jid", "req", "reqid", "requisitionid", "for", "token"}

# Words a title carries that are not the role.
TITLE_NOISE = re.compile(
    r"\[[^\]]*\]|\b(remote|hybrid|onsite|on-site|all levels|new grad|"
    r"early career|us|usa|united states|full[- ]time|part[- ]time|contract)\b|"
    r"\s[-|–—@]\s.*$",
    re.I,
)
COMPANY_NOISE = re.compile(r"\b(inc|llc|ltd|limited|corp|corporation|co|company|careers|jobs)\b\.?", re.I)


@dataclass
class Match:
    level: str  # "high" or "confident"
    id: str
    status: str
    at: str
    title: str
    company: str
    reason: str
    # Why it was rejected, when it was. The banner says "Rejected · visa"
    # rather than "already in autopilot": on a posting the person is
    # looking at again, the outcome is the answer and the plumbing is not.
    reject: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def is_source_page(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in SOURCE_HOSTS)


def canonical(url: str) -> str:
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower().removeprefix("www.")
    path = re.sub(r"/+$", "", parts.path) or "/"
    query = parse_qs(parts.query, keep_blank_values=False)
    kept = sorted((k.lower(), v[0]) for k, v in query.items() if k.lower() in KEEP_PARAMS)
    tail = "&".join(f"{k}={v}" for k, v in kept)
    return f"{host}{path}" + (f"?{tail}" if tail else "")


def ats_id(url: str) -> Optional[tuple[str, str]]:
    for system, pattern in ATS_IDS:
        match = pattern.search(url)
        if match:
            return system, match.group(1)
    return None


def norm_title(title: str) -> str:
    text = TITLE_NOISE.sub(" ", title or "")
    text = re.sub(r"[^a-z0-9 ]", " ", text.lower())
    return " ".join(text.split())


def norm_company(company: str) -> str:
    text = COMPANY_NOISE.sub(" ", company or "")
    text = re.sub(r"[^a-z0-9 ]", " ", text.lower())
    return " ".join(text.split())


def title_similarity(a: str, b: str) -> float:
    a, b = norm_title(a), norm_title(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ta, tb = set(a.split()), set(b.split())
    jaccard = len(ta & tb) / len(ta | tb)
    return max(jaccard, SequenceMatcher(None, a, b).ratio())


def company_from_title(title: str) -> str:
    """"Role @ Company | Site", "Role - Company Careers", "Role | Company"."""
    match = re.search(r"\s@\s(.+?)(?:\s[|–—-]\s|$)", title or "")
    if match:
        return match.group(1).strip()
    parts = re.split(r"\s[|–—-]\s", title or "")
    if len(parts) >= 2:
        tail = parts[-1].strip()
        if not re.search(r"jobright|linkedin|indeed|glassdoor|job recommendations", tail, re.I):
            return re.sub(r"\s+(careers|jobs)$", "", tail, flags=re.I)
    return ""


@dataclass
class Known:
    """One row we have, from the queue or the archive."""
    id: str
    url: str
    title: str
    company: str
    status: str
    at: str
    folder: Optional[Path]
    reject: str = ""

    @property
    def decided_at(self) -> str:
        """When it was submitted or rejected, else when it was added.

        `at` is `added_at`, which is when the posting was noticed. A person
        looking at a posting again is asking when they applied, and the two
        are days apart.
        """
        wanted = (Status.SUBMITTED if self.status == Status.SUBMITTED.value
                  else Status.SKIPPED if self.status == Status.SKIPPED.value else None)
        if wanted and self.folder:
            return store.reached_at(self.folder, wanted) or self.at
        return self.at

    def text(self) -> str:
        if self.folder:
            path = self.folder / "posting.md"
            if path.exists():
                return path.read_text(errors="replace").partition("\n---\n")[2]
        return ""


def known() -> list[Known]:
    rows: dict[str, Known] = {}
    for job in queue.all_jobs():
        rows[job.id] = Known(job.id, job.url, job.title, job.company, job.status.value,
                             job.added_at, Path(job.app_dir) if job.app_dir else None,
                             REJECT_LABELS.get(job.reject_reason, "") if job.reject_reason else "")
    if store.INDEX_PATH.exists():
        # Rows swept out of the queue live on in the archive index.
        for row in csv.DictReader(store.INDEX_PATH.open()):
            if row["job_id"] in rows:
                continue
            rows[row["job_id"]] = Known(row["job_id"], row["url"], row["title"], row["company"],
                                        row["status"], row["created_at"],
                                        store.APPLICATIONS / row["folder"])
    return list(rows.values())


def expired(item: Known) -> bool:
    """A rejection old enough to stop matching: the same title at the same
    company a season later is a new opening, not the one that was turned
    down."""
    if item.status != Status.SKIPPED.value:
        return False
    try:
        then = datetime.fromisoformat(item.decided_at)
    except ValueError:
        return False
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - then > timedelta(days=REJECT_TTL_DAYS)


# Several of our rows can match one posting: the Jobright page and the
# employer page, an old application and a new duplicate someone added
# today. The person is asking one question - what happened with this job -
# and the honest answer is the row that got furthest, not the newest one.
# A duplicate added this morning is "in autopilot" and the application sent
# last week is "applied"; showing the first and hiding the second is how
# "already applied" pointed at a job that had not been.
APPLIED_FIRST = {
    Status.SUBMITTED.value: 0,
    Status.FILLED.value: 1,
    Status.FILLING.value: 2,
}


def _best(item: "Known") -> tuple:
    return (
        APPLIED_FIRST.get(item.status, 3),
        is_source_page(item.url),                  # the employer's row over Jobright's
        item.status == Status.SKIPPED.value,       # a live row over a rejected duplicate
    )


def furthest(items: list[Known]) -> Known:
    """The row that got furthest through the pipeline, which is the answer to
    "what happened with this job?" - see `APPLIED_FIRST`."""
    return sorted(items, key=_best)[0]


def _find_by_scan(url: str, title: str = "", company: str = "", text: str = "") -> Optional[Match]:
    """The original answer: every row, scanned, four signals.

    Kept as the fallback for a lookup `identity/` could not answer. It is
    the implementation this module shipped with, and its measured gaps are
    why `find` now asks the index first - a `/apply` tail on one of two
    otherwise identical URLs let the same Vagaro posting into the queue
    twice, four minutes apart.
    """
    rows = known()
    url_key = canonical(url)
    jr = postings.source_id(url)
    ats = ats_id(url)

    def result(level: str, item: Known, reason: str) -> Match:
        return Match(level, item.id, item.status, item.decided_at, item.title, item.company,
                     reason, item.reject)

    # Strongest signal first across every row, so the employer's own row
    # wins over the Jobright row that led to it.
    checks = [
        (lambda item: canonical(item.url) == url_key, "same page"),
        (lambda item: bool(ats) and ats_id(item.url) == ats, "same job at the employer"),
        (lambda item: bool(jr) and (postings.source_id(item.url) == jr
                                    or ats_id(item.url) == ("jobright", jr)),
         "same Jobright posting"),
    ]
    for check, reason in checks:
        hits = [item for item in rows if check(item)]
        if hits:
            hits.sort(key=_best)
            return result("high", hits[0], reason)

    if not company:
        company = postings.source_meta(url)[1] or company_from_title(title)
    if not title:
        title = postings.source_meta(url)[0]
    mine = norm_company(company)
    if not mine:
        return None
    my_hash = quality.simhash(text) if text else 0
    hits: list[tuple[Known, str]] = []
    for item in rows:
        if norm_company(item.company) != mine or expired(item):
            continue
        if title_similarity(title, item.title) >= TITLE_SIMILARITY:
            hits.append((item, "same role at the same company"))
        elif my_hash:
            theirs = item.text()
            if theirs and quality.distance(my_hash, quality.simhash(theirs)) <= TEXT_DISTANCE:
                hits.append((item, "same description at the same company"))
    if hits:
        # Same choice as above: the one that was applied for is the answer,
        # not whichever row the loop reached first.
        hits.sort(key=lambda pair: _best(pair[0]))
        item, why = hits[0]
        return result("confident", item, why)
    return None




# ---------------------------------------------------------------- the index
#
# `identity/` answers the same question over an index of the same data,
# with a wider ladder: the canonical URL with its `/apply` tails stripped,
# the board's own id across nineteen systems, Jobright's posting id, then
# company-and-title and a posting simhash behind thresholds measured on
# this corpus. It is read-only on the queue and the applications, it checks
# its own staleness on every lookup, and it answers in single-digit
# milliseconds.
#
# This module keeps its own shape - `Match`, `Known`, `furthest`,
# `canonical` are used by the banner, the review page and the Simplify tab -
# and hands the matching itself over. A lookup that raises falls back to
# the scan rather than answering "nothing matched", because a false CLEAR
# is the one answer that costs a second application.

# The index may not import the modules that write the queue and the
# archive (invariant 8), so it asks for their locations through a seam and
# this is where the seam is filled. Registered as callables rather than
# values because both are monkeypatched - by the suite, and by anything
# that runs with a different data directory - after import.
identity_sources.PATH_HOOKS["queue"] = lambda: queue.QUEUE_PATH
identity_sources.PATH_HOOKS["applications"] = lambda: store.APPLICATIONS

CERTAIN_LEVEL = "high"
LIKELY_LEVEL = "confident"


def _reject_label(job_id: str) -> str:
    """The reason a rejected row was rejected, in the banner's words.

    The index does not carry it: a reject reason is autopilot's own
    vocabulary, not part of a posting's identity.
    """
    for job in queue.all_jobs():
        if job.id == job_id and job.reject_reason:
            return REJECT_LABELS.get(job.reject_reason, "")
    return ""


def find(url: str, title: str = "", company: str = "", text: str = "") -> Optional[Match]:
    """The best match for this page, or None.

    The enrichment stays here, before the lookup: Jobright's own page says
    the company and the role in its title and nowhere else, and the index
    is given facts rather than asked to guess them.
    """
    if not company:
        company = postings.source_meta(url)[1] or company_from_title(title)
    if not title:
        title = postings.source_meta(url)[0]
    try:
        verdict = identity.lookup(url, title=title, company=company, text=text)
    except Exception:                       # noqa: BLE001 - the scan is the fallback
        return _find_by_scan(url, title=title, company=company, text=text)
    if not verdict.matches:
        return None
    best = verdict.matches[0]
    level = CERTAIN_LEVEL if best.confidence == identity.CERTAIN else LIKELY_LEVEL
    return Match(level, best.display_id, best.status, best.decided_at, best.title,
                 best.company, best.rule, _reject_label(best.display_id))


def _employer_jobright_id(url: str) -> str:
    """Jobright's posting id, but not from Jobright's own page.

    Two employer URLs carrying the same `jr_id` are one posting and one
    row. Jobright's posting page and the employer page its Apply opens are
    also one posting - the banner says so, and `find` ranks the employer's
    row first - but they are not one **row**: the employer row is the one
    the fill works on, and refusing it because a Jobright row happens to
    exist would mean the application was never queued at all.
    """
    return "" if is_source_page(url) else identity.keys.jobright_id(url)


def same_job(a: Job, b: Job) -> bool:
    """Provably one posting: what `queue.add` refuses to duplicate.

    `identity.keys` is asked, because this module's own canonical form let
    a duplicate through: Vagaro's posting arrived once as
    `.../p/<id>/apply` and once as `.../p/<id>`, four minutes apart, and
    both were queued - one row failed, the other submitted. Stripping the
    application tail is one of the things the index's canonical form does
    that this one did not, and Breezy is one of the fifteen systems it
    recognises an id for that `ats_id` does not.

    Only a proof counts here. Company-and-title resemblance is for a
    person reading a banner, never for refusing a job silently at the
    door.
    """
    if identity.keys.canonical(a.url) == identity.keys.canonical(b.url):
        return True
    ja, jb = _employer_jobright_id(a.url), _employer_jobright_id(b.url)
    if ja and ja == jb:
        return True
    aa, ab = identity.keys.ats_identity(a.url), identity.keys.ats_identity(b.url)
    return bool(aa) and aa == ab
