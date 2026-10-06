"""The four job boards that publish the posting themselves.

Measured over the 285 application folders on disk (2026-10-06). Jobright's
Apply does not land on a job description: it lands on the *application*, and
the scraper then reads the form. Of the 134 distinct Ashby / Greenhouse /
Lever / Workday URLs captured, 109 were a form URL
(`/embed/job_app`, `/apply`, `/application`), and what that cost was not a
missing posting so much as a posting buried in one:

* **Greenhouse** `/embed/job_app` is the form alone on some boards (ASM: 524
  words, not one section heading, tailored anyway) and the description plus
  the whole form on others (Axon: 3,381 words, of which 1,617 are the
  posting and the rest are "AttachAttach", "Accepted file types", the
  demographic questions and the EEO statement).
* **Lever** `/apply` is the form, and a fetch of it on a board with a list
  page behind it read the entire board: Palantir came back as 10,832 words
  of other people's jobs.
* **Ashby** `/application` truncates the description on some boards (Clay:
  the page's own copy is missing the half of the posting the board publishes)
  and appends the self-identification form on all of them.
* **Workday** renders client-side; the fetch falls back to the JSON-LD block,
  whose `description` Workday has already stripped of its own markup - 6,367
  characters on **one line**, every bullet run into the sentence before it.

All four publish the posting over an endpoint that needs no login and no
key, and all four answer with the description and nothing else, markup
intact. That is what this module reads. A URL it does not recognise, a board
that answers anything but 200, or a job that is no longer listed returns
None and the ordinary fetch runs exactly as before.
"""

from __future__ import annotations

import html as _html
import re
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import parse_qs, urlsplit

import httpx

from .fetch import Posting, USER_AGENT, _strip_html

TIMEOUT = 20.0
HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/json"}

GREENHOUSE = "https://boards-api.greenhouse.io/v1/boards/{org}/jobs/{job}"
LEVER = "https://api.lever.co/v0/postings/{org}/{job}"
ASHBY = "https://api.ashbyhq.com/posting-api/job-board/{org}?includeCompensation=true"
WORKDAY = "https://{host}/wday/cxs/{tenant}/{site}{path}"

_UUID = r"[0-9a-fA-F-]{36}"


@dataclass
class Board:
    """Which board a URL belongs to, and the two names it is known by there."""
    system: str
    org: str
    job: str
    # Workday needs the host and the site as well as the tenant.
    host: str = ""
    site: str = ""


def identify(url: str) -> Optional[Board]:
    """The board behind `url`, whether it points at the posting or the form."""
    if not url:
        return None
    parts = urlsplit(url)
    host, path = parts.netloc.lower(), parts.path
    query = parse_qs(parts.query)

    if "greenhouse.io" in host:
        # The embed is one path for every job; `for` and `token` are the names.
        org = (query.get("for") or [""])[0]
        job = (query.get("token") or query.get("gh_jid") or [""])[0]
        if not (org and job):
            found = re.match(rf"^/(?:embed/)?([^/]+)/jobs/(\d+)", path)
            if not found:
                return None
            org, job = found.group(1), found.group(2)
        return Board("greenhouse", org, job)

    if "lever.co" in host:
        found = re.match(rf"^/([^/]+)/({_UUID})", path)
        return Board("lever", found.group(1), found.group(2)) if found else None

    if "ashbyhq.com" in host:
        found = re.match(rf"^/([^/]+)/({_UUID})", path)
        return Board("ashby", found.group(1), found.group(2)) if found else None

    if "myworkdayjobs.com" in host:
        # `/<site>/job/<place>/<title>_<id>`, with an optional `/en-US` in
        # front of it and an optional `/apply` behind it. The tenant is the
        # subdomain on every tenant measured; one that is not falls through
        # to the ordinary fetch, which still has the JSON-LD block.
        found = re.match(r"^(?:/[a-z]{2}-[A-Za-z]{2})?/([^/]+)(/job/[^?]+)", path)
        if not found:
            return None
        site, job = found.group(1), re.sub(r"/apply.*$", "", found.group(2))
        return Board("workday", host.split(".")[0], job, host=parts.netloc, site=site)

    return None


def _text(markup: str | None) -> str:
    return _strip_html(_html.unescape(markup or ""))


def _greenhouse(board: Board, client: httpx.Client) -> Optional[Posting]:
    data = client.get(GREENHOUSE.format(org=board.org, job=board.job)).raise_for_status().json()
    text = _text(data.get("content"))
    if not text:
        return None
    return Posting(url="", text=text, title=data.get("title") or "",
                   location=(data.get("location") or {}).get("name") or "")


def _lever(board: Board, client: httpx.Client) -> Optional[Posting]:
    data = client.get(LEVER.format(org=board.org, job=board.job)).raise_for_status().json()
    # A Lever posting is an opening paragraph, then named lists
    # ("Responsibilities", "Requirements"), then a closing note - and a
    # posting may have no paragraph at all, only the lists.
    blocks = [_text(data.get("opening")), _text(data.get("description"))]
    for section in data.get("lists") or []:
        blocks.append(section.get("text") or "")
        blocks.append(_text(section.get("content")))
    blocks.append(_text(data.get("additional")))
    text = "\n\n".join(block for block in blocks if block.strip())
    if not text:
        return None
    categories = data.get("categories") or {}
    return Posting(url="", text=text, title=data.get("text") or "",
                   location=categories.get("location") or "")


def _ashby(board: Board, client: httpx.Client) -> Optional[Posting]:
    # Ashby publishes the board, not the job; the one we want is in it while
    # it is open, and gone from it once it closes.
    for job in client.get(ASHBY.format(org=board.org)).raise_for_status().json().get("jobs") or []:
        if job.get("id") != board.job:
            continue
        text = _text(job.get("descriptionHtml")) or (job.get("descriptionPlain") or "")
        if not text:
            return None
        return Posting(url="", text=text, title=job.get("title") or "",
                       location=job.get("location") or "")
    return None


def _workday(board: Board, client: httpx.Client) -> Optional[Posting]:
    data = client.get(WORKDAY.format(host=board.host, tenant=board.org,
                                     site=board.site, path=board.job)).raise_for_status().json()
    info = data.get("jobPostingInfo") or {}
    text = _text(info.get("jobDescription"))
    if not text:
        return None
    return Posting(url="", text=text, title=info.get("title") or "",
                   location=info.get("location") or "")


READERS: dict[str, Callable[[Board, httpx.Client], Optional[Posting]]] = {
    "greenhouse": _greenhouse,
    "lever": _lever,
    "ashby": _ashby,
    "workday": _workday,
}


def posting(url: str, client: httpx.Client | None = None) -> Optional[Posting]:
    """The posting behind a job-board URL, or None when there is not one.

    Raises nothing: a board that is down, a job that has closed and a URL
    shaped differently all fall back to the ordinary fetch, which is what
    happened before this module existed.
    """
    board = identify(url)
    if board is None:
        return None
    try:
        owned = client is None
        client = client or httpx.Client(follow_redirects=True, timeout=TIMEOUT,
                                        headers=HEADERS)
        try:
            found = READERS[board.system](board, client)
        finally:
            if owned:
                client.close()
    except Exception:  # noqa: BLE001 - the ordinary fetch is the fallback
        return None
    if found is None:
        return None
    found.url = url
    # The board's own copy of its own posting. Nothing scraped off a page
    # competes with it in `pipeline.fetch_posting`.
    found.authoritative = True
    found.text_source = f"{board.system}'s job board"
    if not found.company:
        found.company = board.org
    return found
