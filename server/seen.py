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

from archive import store
from tailor import quality

from . import postings, queue
from .models import Job, Status

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
                             job.added_at, Path(job.app_dir) if job.app_dir else None)
    if store.INDEX_PATH.exists():
        # Rows swept out of the queue live on in the archive index.
        for row in csv.DictReader(store.INDEX_PATH.open()):
            if row["job_id"] in rows:
                continue
            rows[row["job_id"]] = Known(row["job_id"], row["url"], row["title"], row["company"],
                                        row["status"], row["created_at"],
                                        store.APPLICATIONS / row["folder"])
    return list(rows.values())


def _expired(item: Known) -> bool:
    if item.status != Status.SKIPPED.value:
        return False
    try:
        then = datetime.fromisoformat(item.at)
    except ValueError:
        return False
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - then > timedelta(days=REJECT_TTL_DAYS)


def find(url: str, title: str = "", company: str = "", text: str = "") -> Optional[Match]:
    """The best match for this page, or None."""
    rows = known()
    url_key = canonical(url)
    jr = postings.source_id(url)
    ats = ats_id(url)

    def result(level: str, item: Known, reason: str) -> Match:
        return Match(level, item.id, item.status, item.at, item.title, item.company, reason)

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
            # The employer's row over the Jobright row that led to it, and a
            # live row over a rejected duplicate.
            hits.sort(key=lambda item: (is_source_page(item.url), item.status == Status.SKIPPED.value))
            return result("high", hits[0], reason)

    if not company:
        company = postings.source_meta(url)[1] or company_from_title(title)
    if not title:
        title = postings.source_meta(url)[0]
    mine = norm_company(company)
    if not mine:
        return None
    my_hash = quality.simhash(text) if text else 0
    for item in rows:
        if norm_company(item.company) != mine or _expired(item):
            continue
        if title_similarity(title, item.title) >= TITLE_SIMILARITY:
            return result("confident", item, "same role at the same company")
        if my_hash:
            theirs = item.text()
            if theirs and quality.distance(my_hash, quality.simhash(theirs)) <= TEXT_DISTANCE:
                return result("confident", item, "same description at the same company")
    return None


def same_job(a: Job, b: Job) -> bool:
    """Provably one posting: what `queue.add` refuses to duplicate."""
    if canonical(a.url) == canonical(b.url):
        return True
    ja, jb = postings.source_id(a.url), postings.source_id(b.url)
    if ja and ja == jb:
        return True
    aa, ab = ats_id(a.url), ats_id(b.url)
    return bool(aa) and aa == ab
