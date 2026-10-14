"""Board scan: the jobs, read from every company's own board, the day they go up.

Greenhouse, Lever and Ashby each publish a public, keyless endpoint that
answers with every open role at *one* company:

    https://boards-api.greenhouse.io/v1/boards/<org>/jobs?content=true
    https://api.lever.co/v0/postings/<org>?mode=json
    https://api.ashbyhq.com/posting-api/job-board/<org>

What none of the three publishes is an endpoint that searches *across*
their customers. There is no `?q=new+grad` over all of Greenhouse, for
anybody. That is why the usual trick for this is a Google query
(`site:job-boards.greenhouse.io "new grad"`): Google crawled the pages, so
Google's index is the only cross-company index that exists. Borrowing it
costs what Google's index costs - a fraction of the postings, days late,
and about a hundred results to a query.

So this package keeps its own index of the one thing the endpoints need:
the company's name as the board spells it, the `org` above, called a slug
throughout. With the slug list in hand every board answers in full, in
milliseconds, for free, the moment a role is posted.

The slugs are harvested from the public new-grad and internship lists on
GitHub (`sources.py`), which carry tens of thousands of apply links
between them; a link to any of the three boards contains the slug, and
`scout.detect` already knows how to read one out of every URL shape those
boards use. An old list is as useful as a current one here: a posting from
2023 is long dead, but the company still has a board. Measured on the
first harvest (2026-10-14): nine lists, 80,786 links, **1,979 distinct
companies** - 901 Greenhouse, 698 Ashby, 380 Lever - of which a sample of
75 found 72 boards still answering, carrying 3,570 open roles, 130 of them
at the applicant's level and in the US, every one with its description.

This is the back half of the job search, and it is deliberately separate
from autopilot: a server of its own on its own port, writing its own
store, reading autopilot's data but never writing it. Nothing here fills a
form, presses a button or opens a tab. It collects, filters and lists; the
person reads the list and decides. What they decline is kept with the
reason, because the reject pile is what tightens the filters.
"""
from __future__ import annotations

import hashlib
from typing import Optional

from pydantic import BaseModel, Field

from scout import DEFAULT_NEGATIVE, DEFAULT_POSITIVE
from server.models import utcnow

# The three boards this package reads. Each answers with every open role at
# one company, needs no key and no login, and is the same endpoint the
# company's own careers page calls. Workday is deliberately absent: it is
# the biggest employers and the worst API, and it needs a tenant and a site
# rather than a slug, so it is its own piece of work.
SYSTEMS = ("greenhouse", "lever", "ashby")

# Where a slug points when a person wants to look at the board by hand.
BOARD_URL = {
    "greenhouse": "https://job-boards.greenhouse.io/{slug}",
    "lever": "https://jobs.lever.co/{slug}",
    "ashby": "https://jobs.ashbyhq.com/{slug}",
}


class Company(BaseModel):
    """One board we know how to read, and how it has been behaving.

    `dead` is set when the board answers 404: the company has left that
    system (or never had a board there, since a slug harvested from a link
    is a guess until it is asked). A dead company is kept rather than
    deleted, so the next harvest does not offer it again as news, and so a
    board that comes back can be noticed.
    """

    system: str
    slug: str
    name: str = ""                      # the company as a list named it, for the page
    sources: list[str] = Field(default_factory=list)   # which lists this slug came from
    added_at: str = Field(default_factory=utcnow)
    checked_at: Optional[str] = None
    ok_at: Optional[str] = None         # last time it answered
    roles: int = 0                      # open roles on the last good read
    at_level: int = 0                   # of those, past the code filters
    error: Optional[str] = None         # the last failure, cleared by a good read
    dead: bool = False
    enabled: bool = True

    @property
    def id(self) -> str:
        return f"{self.system}:{self.slug}"

    @property
    def url(self) -> str:
        return BOARD_URL[self.system].format(slug=self.slug)


class Role(BaseModel):
    """One open role as a board lists it, plus what we made of it.

    The identity fields are the board's own (`system`, `slug`, `board_id`),
    so the same role read tomorrow is recognised as the same row without
    comparing text. `verdict` and `flags` are the code filters' output;
    `state` is the person's, and `reason` is why they declined - the pile
    that teaches the next round of filters.
    """

    system: str
    slug: str
    board_id: str                       # the board's own id for the role
    company: str = ""
    title: str = ""
    url: str = ""
    location: str = ""
    posted: str = ""                    # ISO date the board gives, else ""
    text: str = ""
    found_at: str = Field(default_factory=utcnow)

    verdict: str = ""                   # ok | caution | reject, from the code screen
    flags: list[str] = Field(default_factory=list)      # which rules fired, for the page
    seen_verdict: str = ""              # already in autopilot: high | confident | ""
    seen_job: Optional[str] = None      # which job of ours it matched

    state: str = "new"                  # new | added | declined
    reason: str = ""                     # free text when declined
    decided_at: Optional[str] = None

    @property
    def id(self) -> str:
        """Stable across reads: the board's own identity, hashed short for a URL."""
        return hashlib.sha256(f"{self.system}:{self.slug}:{self.board_id}".encode()).hexdigest()[:12]


# Boards answer in well under a second and the crawl is nothing but
# waiting on them; twelve at a time read 75 boards in 7.3 seconds
# (measured 2026-10-14) and keeps the whole registry to about three
# minutes. Higher is not obviously better: these are other people's
# public endpoints, and this runs once a day.
DEFAULT_WORKERS = 12

# The level filter's word lists. Seeded from Scout's, which are shared with
# the watches and are not this package's to change, plus the negatives this
# crawl measured on its first real read (2026-10-14): `engineer i` and
# `engineer ii` are in the positive list because that is what a new-grad
# software title often is, and on 40 boards they also brought in
# "Manufacturing Engineer II", "Vehicle Fluids Engineer II", "Device
# Engineer II" and "Engineer II, Avionics Thermal Engineering" - other
# people's professions, every one of them then rejected by the years rule,
# which is a model-free rejection but still a row to read.
#
# A word removed from here is a role that will be listed again. Nothing is
# added on a hunch: a new negative belongs here once the decline pile shows
# the same wrong title twice.
LEVEL_POSITIVE = list(DEFAULT_POSITIVE)
LEVEL_NEGATIVE = list(DEFAULT_NEGATIVE) + [
    "manufacturing", "avionics", "thermal", "fluids", "structures",
    "structural", "propulsion", "hardware", "firmware", "rf ", "optical",
    "chemical", "civil", "industrial", "quality", "process engineer",
    "test engineer", "validation", "systems engineer", "network engineer",
    "security engineer", "sales engineer", "support engineer",
    "solutions engineer", "customer engineer", "field engineer",
]
