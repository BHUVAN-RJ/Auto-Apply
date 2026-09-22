"""Scout: watch a company's own careers page for new roles at the
applicant's level and say so, by mail and on the review page.

A watch is one careers URL. `detect` reads the host and path and picks
the provider (the public JSON behind Greenhouse, Lever, Ashby, Workday,
SmartRecruiters, Oracle, Eightfold; the server-rendered search pages
of Google, Amazon, Apple) so that pasting the page you would open by
hand is the whole setup. Every provider returns the same `Posting`;
`scout.run` filters titles by level, drops what the watch has already
seen, screens the rest with the same on-page screen the banner uses,
records hits and mails a digest. A watch is one of two kinds
(2026-09-21): `notify` watches are the companies where the person can
get a referral, so their hits are only listed and mailed with the link,
for the person to ask; every other watch's hits go straight into
autopilot when the screen does not reject them (`run.check` calls
`queue_hit`, the same three steps as `/capture`), and the pipeline
takes them from there. Nothing here presses Submit: the fill stops at
the filled form as it does for any job.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from typing import Optional
from urllib.parse import parse_qs, urlparse

from pydantic import BaseModel, Field

from server.models import utcnow


@dataclass
class Posting:
    """One role as a provider lists it. `text` is whatever description
    the listing carried (Google, Amazon, Greenhouse with content, Lever,
    Ashby all send it); empty means the screen fetches the page."""

    id: str
    title: str
    url: str
    location: str = ""
    posted: str = ""      # ISO date when the provider says, else ""
    text: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# Entry-level software titles and the roles next door (data, backend,
# full stack, platform, ML, infrastructure). `positive`: at least one
# must appear in the title; `negative`: none may. Case-insensitive
# substring matches; a watch can override both lists.
DEFAULT_POSITIVE = [
    "software engineer", "software developer", "software development engineer",
    "software engineering", "swe", "sde", "new grad", "early career", "university grad",
    "engineer i", "engineer ii", "associate engineer", "junior", "entry level", "graduate",
    "data engineer", "backend engineer", "back-end engineer", "back end engineer",
    "full stack", "full-stack", "fullstack", "frontend engineer", "front-end engineer",
    "platform engineer", "infrastructure engineer", "machine learning engineer",
    "ml engineer", "ai engineer", "cloud engineer", "site reliability", "devops engineer",
    "member of technical staff", "applied scientist", "data scientist",
    "frontend developer", "front-end developer", "backend developer", "back-end developer",
    "full stack developer", "full-stack developer", "web developer", "application developer",
]
DEFAULT_NEGATIVE = [
    "senior", "staff", "principal", "lead", "manager", "director", "head of",
    "vp", "vice president", "architect", "intern", "sr.", "sr ", "distinguished",
    "fellow",
    # Not software: "Electrical Controls Engineer II" passed on "engineer ii".
    "electrical", "mechanical", "controls", "technician", "sales", "recruiter",
    "accountant", "marketing", "counsel", "field service",
]
# "III" is not in the list on purpose: Google's Software Engineer III is
# its new-grad level. A watch on a company where III means mid-level adds
# it to its own negatives.

DEFAULT_CHECKS_PER_DAY = 4

LOCALE = re.compile(r"^[a-z]{2}-[A-Z]{2}$")


class Watch(BaseModel):
    url: str
    company: str = ""
    provider: str = ""
    args: dict = Field(default_factory=dict)   # provider-specific: slug, host, tenant, site, domain
    query: str = ""                            # server-side search text where the provider takes one
    positive: list[str] = Field(default_factory=lambda: list(DEFAULT_POSITIVE))
    negative: list[str] = Field(default_factory=lambda: list(DEFAULT_NEGATIVE))
    checks_per_day: Optional[int] = None       # None = the global setting
    enabled: bool = True
    notify: bool = False                       # True: list and mail only, the person asks for a referral; False: hits go into autopilot
    us_only: bool = True                       # drop roles whose location names another country before anything else
    listed: list[dict] = Field(default_factory=list)   # the roles at the level on the last check: title, url, location, posted, id
    referrer: str = ""                         # who to ask for a referral, free text
    added_at: str = Field(default_factory=utcnow)
    last_checked: Optional[str] = None
    last_ok: Optional[str] = None
    last_total: int = 0                        # roles the provider listed on the last check
    last_matching: int = 0                     # of those, at the applicant's level
    error: Optional[str] = None                # the broken banner; None when the last check worked
    broken_mailed: bool = False                # the "does not work" mail went; reset when it reads again
    seen_ids: list[str] = Field(default_factory=list)

    @property
    def id(self) -> str:
        return hashlib.sha256(self.url.encode()).hexdigest()[:6]

    @property
    def broken(self) -> bool:
        return self.error is not None


class Hit(BaseModel):
    """A new role at the level, kept until the person acts on it."""

    watch_id: str
    company: str
    posting: dict                               # Posting.to_dict()
    found_at: str = Field(default_factory=utcnow)
    verdict: str = ""                           # the screen's, "" when the screen failed
    summary: str = ""
    status: str = "new"                         # new | queued | dismissed
    job_id: Optional[str] = None
    mailed: bool = False

    @property
    def id(self) -> str:
        return hashlib.sha256(f"{self.watch_id}:{self.posting['id']}".encode()).hexdigest()[:8]


class DetectError(ValueError):
    pass


def detect(url: str) -> tuple[str, dict, str]:
    """(provider, args, company guess) for a careers URL. Raises DetectError
    when no provider serves the host: the watch is not created, since a
    watch that can never list a role is worse than none. `args["query"]`,
    when present, is the search text the page itself carried; the caller
    moves it to `Watch.query`."""
    parsed = urlparse(url if "://" in url else "https://" + url)
    host = parsed.netloc.lower()
    parts = [p for p in parsed.path.split("/") if p]
    query = parse_qs(parsed.query)

    if host.endswith("greenhouse.io"):
        slug = (query.get("for") or [None])[0]
        if not slug:
            slug = parts[1] if parts and parts[0] == "embed" and len(parts) > 1 else (parts[0] if parts else "")
        if not slug:
            raise DetectError("Greenhouse URL has no board name")
        return "greenhouse", {"slug": slug}, slug
    if host.endswith("ashbyhq.com"):
        if not parts:
            raise DetectError("Ashby URL has no board name")
        return "ashby", {"slug": parts[0]}, parts[0]
    if host.endswith("bamboohr.com"):
        sub = host.split(".")[0]
        if not sub or sub in ("www", "bamboohr"):
            raise DetectError("BambooHR URL needs the company subdomain, e.g. https://acme.bamboohr.com/careers")
        return "bamboohr", {"slug": sub}, sub
    if host.endswith("lever.co"):
        if not parts:
            raise DetectError("Lever URL has no board name")
        return "lever", {"slug": parts[0]}, parts[0]
    if "myworkdayjobs.com" in host or "myworkdaysite.com" in host:
        tenant = host.split(".")[0]
        site = next((p for p in parts if not LOCALE.match(p)), "")
        if not site:
            raise DetectError("Workday URL needs the site after the host, e.g. /en-US/External_Careers")
        return "workday", {"host": host, "tenant": tenant, "site": site}, tenant
    if host.endswith("smartrecruiters.com"):
        if not parts:
            raise DetectError("SmartRecruiters URL has no company")
        return "smartrecruiters", {"company": parts[0]}, parts[0]
    if host.endswith("oraclecloud.com"):
        site = parts[parts.index("sites") + 1] if "sites" in parts and parts.index("sites") + 1 < len(parts) else ""
        if not site:
            raise DetectError("Oracle URL needs /sites/<site> in the path")
        return "oracle", {"host": host, "site": site}, host.split(".")[0]
    q = (query.get("q") or query.get("search") or query.get("base_query") or [""])[0]
    if host in ("careers.google.com", "www.google.com", "google.com") and (
            host == "careers.google.com" or "careers" in parts):
        # The page's own filters carry over: q, location, target_level.
        args = {"query": q}
        for key in ("location", "target_level"):
            if query.get(key):
                args[key] = query[key][0]
        return "google", args, "Google"
    if host.endswith("amazon.jobs"):
        return "amazon", {"query": q, **({"country": query["country"][0]} if query.get("country") else {})}, "Amazon"
    if host.endswith("jobs.apple.com"):
        return "apple", {"query": q}, "Apple"
    if host.endswith("metacareers.com"):
        raise DetectError("Meta's careers site needs a session token per request; not supported")
    domain = (query.get("domain") or [None])[0]
    if host.endswith("eightfold.ai") or domain or host.endswith("careers.microsoft.com"):
        if host.endswith("careers.microsoft.com"):
            host, domain = "apply.careers.microsoft.com", domain or "microsoft.com"
        if not domain:
            raise DetectError("Eightfold URL needs ?domain=<company domain>")
        return "eightfold", {"host": host, "domain": domain}, domain.split(".")[0]
    raise DetectError(f"No provider for {host}. Supported: Greenhouse, Lever, Ashby, BambooHR, Workday, "
                      "SmartRecruiters, Oracle, Eightfold/Microsoft, Google, Amazon, Apple")
