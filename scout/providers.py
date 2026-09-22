"""One function per careers system, each returning the same `Posting`
rows, newest first where the system can sort.

Every function takes the watch (its `args` and `query`) and an httpx
client, and raises on failure: the caller records the error on the watch
and shows it red. A provider never returns half a list quietly.

The endpoints are the public JSON the pages themselves call (Greenhouse,
Lever, Ashby, BambooHR, Workday, SmartRecruiters, Oracle, Eightfold) or the
server-rendered search page where there is none (Google, Amazon, Apple).
Google's old `careers.google.com/api/v3/search` answers `Not Found` since
2026; its results page embeds the same rows, description included, in
an `AF_initDataCallback` block.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from html import unescape
from typing import Callable
from urllib.parse import urlencode

import httpx

from scout import Posting, Watch

USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
TIMEOUT = 30.0
PAGE = 100          # rows per request where the system pages
MAX_ROWS = 300      # newest rows worth looking at; a watch runs several times a day

TAGS = re.compile(r"<[^>]+>")


def strip_html(value: str) -> str:
    return unescape(TAGS.sub(" ", value or "")).replace("\xa0", " ").strip()


def _date(value) -> str:
    """ISO date out of an epoch (seconds or milliseconds) or an ISO string."""
    if value is None or value == "":
        return ""
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 10_000_000_000 else value
        return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y-%m-%d")
    value = str(value).strip()
    for form in ("%B %d, %Y", "%b %d, %Y"):     # Amazon: "September 18, 2026"
        try:
            return datetime.strptime(value, form).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return value[:10]


def client() -> httpx.Client:
    return httpx.Client(follow_redirects=True, timeout=TIMEOUT,
                        headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})


def greenhouse(watch: Watch, http: httpx.Client) -> list[Posting]:
    slug = watch.args["slug"]
    data = http.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true").raise_for_status().json()
    out = []
    for j in data.get("jobs", []):
        out.append(Posting(id=str(j.get("id")), title=j.get("title", ""), url=j.get("absolute_url", ""),
                           location=(j.get("location") or {}).get("name", ""),
                           posted=_date(j.get("first_published") or j.get("updated_at")),
                           text=strip_html(j.get("content", ""))))
    out.sort(key=lambda p: p.posted, reverse=True)
    return out


def lever(watch: Watch, http: httpx.Client) -> list[Posting]:
    slug = watch.args["slug"]
    data = http.get(f"https://api.lever.co/v0/postings/{slug}?mode=json").raise_for_status().json()
    out = []
    for j in data:
        cats = j.get("categories") or {}
        text = strip_html(j.get("descriptionPlain") or j.get("description") or "")
        for block in j.get("lists") or []:
            text += "\n" + strip_html(block.get("text", "")) + "\n" + strip_html(block.get("content", ""))
        out.append(Posting(id=str(j.get("id")), title=j.get("text", ""), url=j.get("hostedUrl", ""),
                           location=cats.get("location", ""), posted=_date(j.get("createdAt")), text=text))
    out.sort(key=lambda p: p.posted, reverse=True)
    return out


def bamboohr(watch: Watch, http: httpx.Client) -> list[Posting]:
    """`<sub>.bamboohr.com/careers/list` is the JSON the board page
    itself loads; the description is one more call per role
    (`/careers/<id>/detail`), made only for the ids not yet seen so a
    board of thirty is not thirty calls a check."""
    sub = watch.args["slug"]
    base = f"https://{sub}.bamboohr.com/careers"
    data = http.get(f"{base}/list", headers={"Accept": "application/json"}).raise_for_status().json()
    seen = set(watch.seen_ids)
    out = []
    for j in data.get("result") or []:
        jid = str(j.get("id"))
        loc = j.get("location") or {}
        location = ", ".join(x for x in (loc.get("city"), loc.get("state")) if x)
        if j.get("isRemote"):
            location = (location + "; " if location else "") + "Remote"
        text, posted = "", ""
        if jid not in seen:
            try:
                detail = http.get(f"{base}/{jid}/detail", headers={"Accept": "application/json"}).raise_for_status().json()
                opening = (detail.get("result") or {}).get("jobOpening") or {}
                text = strip_html(opening.get("description") or "")
                posted = opening.get("datePosted") or ""
            except Exception:  # noqa: BLE001 - the listing is enough; the screen fetches the page
                pass
        out.append(Posting(id=jid, title=(j.get("jobOpeningName") or "").strip(), url=f"{base}/{jid}",
                           location=location, posted=posted, text=text))
    out.sort(key=lambda p: p.posted, reverse=True)
    return out


def ashby(watch: Watch, http: httpx.Client) -> list[Posting]:
    slug = watch.args["slug"]
    data = http.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true").raise_for_status().json()
    out = []
    for j in data.get("jobs", []):
        out.append(Posting(id=str(j.get("id")), title=j.get("title", ""),
                           url=j.get("jobUrl") or j.get("applyUrl", ""), location=j.get("location", ""),
                           posted=_date(j.get("publishedAt")), text=strip_html(j.get("descriptionHtml") or j.get("descriptionPlain", ""))))
    out.sort(key=lambda p: p.posted, reverse=True)
    return out


def workday(watch: Watch, http: httpx.Client) -> list[Posting]:
    host, tenant, site = watch.args["host"], watch.args["tenant"], watch.args["site"]
    out = []
    for offset in range(0, MAX_ROWS, 20):
        data = http.post(f"https://{host}/wday/cxs/{tenant}/{site}/jobs",
                         json={"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": watch.query},
                         headers={"Accept": "application/json"}).raise_for_status().json()
        rows = data.get("jobPostings") or []
        for p in rows:
            path = p.get("externalPath", "")
            out.append(Posting(id=(p.get("bulletFields") or [path])[0] or path, title=p.get("title", ""),
                               url=f"https://{host}/en-US/{site}{path}", location=p.get("locationsText", ""),
                               posted=p.get("postedOn", "")))
        if len(rows) < 20:
            break
    return out


def smartrecruiters(watch: Watch, http: httpx.Client) -> list[Posting]:
    company = watch.args["company"]
    out = []
    for offset in range(0, MAX_ROWS, PAGE):
        params = {"limit": PAGE, "offset": offset}
        if watch.query:
            params["q"] = watch.query
        data = http.get(f"https://api.smartrecruiters.com/v1/companies/{company}/postings",
                        params=params).raise_for_status().json()
        rows = data.get("content") or []
        for j in rows:
            loc = j.get("location") or {}
            place = ", ".join(x for x in (loc.get("city"), loc.get("region"), loc.get("country")) if x)
            out.append(Posting(id=str(j.get("id")), title=j.get("name", ""),
                               url=f"https://jobs.smartrecruiters.com/{company}/{j.get('id')}",
                               location=place, posted=_date(j.get("releasedDate"))))
        if len(rows) < PAGE:
            break
    out.sort(key=lambda p: p.posted, reverse=True)
    return out


def oracle(watch: Watch, http: httpx.Client) -> list[Posting]:
    host, site = watch.args["host"], watch.args["site"]
    finder = f"findReqs;siteNumber={site},limit={PAGE},sortBy=POSTING_DATES_DESC"
    if watch.query:
        finder += f",keyword={watch.query}"
    url = (f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
           f"?onlyData=true&expand=requisitionList.secondaryLocations&finder={finder}")
    data = http.get(url, headers={"Accept": "application/json"}).raise_for_status().json()
    items = data.get("items") or [{}]
    out = []
    for r in items[0].get("requisitionList") or []:
        out.append(Posting(id=str(r.get("Id")), title=r.get("Title", ""),
                           url=f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{r.get('Id')}",
                           location=r.get("PrimaryLocation", ""), posted=_date(r.get("PostedDate"))))
    return out


def eightfold(watch: Watch, http: httpx.Client) -> list[Posting]:
    host, domain = watch.args["host"], watch.args["domain"]
    out = []
    # Microsoft's tenant answers ten rows a page whatever `num` says.
    start = 0
    while start < MAX_ROWS:
        params = {"domain": domain, "query": watch.query, "location": "", "start": start,
                  "num": 50, "sort_by": "timestamp"}
        data = http.get(f"https://{host}/api/pcsx/search", params=params,
                        headers={"Accept": "application/json"}).raise_for_status().json()
        rows = (data.get("data") or {}).get("positions") or data.get("positions") or []
        for p in rows:
            rel = p.get("positionUrl") or p.get("canonicalPositionUrl") or f"/careers/job/{p.get('id')}"
            out.append(Posting(id=str(p.get("id")), title=p.get("name", ""),
                               url=rel if rel.startswith("http") else f"https://{host}{rel}",
                               location="; ".join(p.get("locations") or []) or p.get("location", ""),
                               posted=_date(p.get("postedTs") or p.get("creationTs"))))
        if not rows:
            break
        start += len(rows)
    return out


GOOGLE_SEARCH = "https://www.google.com/about/careers/applications/jobs/results/"
GOOGLE_DATA = re.compile(r"AF_initDataCallback\(\{key: 'ds:1'.*?data:(\[.*?\])\s*, sideChannel:", re.S)


def _google_rows(html: str) -> list[Posting]:
    """The results page embeds the rows as positional arrays: id, title,
    apply URL, responsibilities, qualifications, ..., locations at 9,
    description at 10, posted epoch at 12, team at 15, preferred at 18,
    minimum at 19. Field order checked against the live page 2026-09-20."""
    m = GOOGLE_DATA.search(html)
    if not m:
        raise RuntimeError("Google results page has no job data block; the page layout changed")
    data = json.loads(m.group(1))
    rows = data[0] if data and isinstance(data[0], list) else []

    def html_at(row, i):
        cell = row[i] if len(row) > i else None
        return strip_html(cell[1]) if isinstance(cell, list) and len(cell) > 1 and cell[1] else ""

    out = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 3:
            continue
        posted = row[12] if len(row) > 12 else None
        locs = [str(l[0]) for l in (row[9] if len(row) > 9 and isinstance(row[9], list) else []) if isinstance(l, list) and l]
        text = "\n\n".join(x for x in (
            html_at(row, 10), "Minimum qualifications:\n" + html_at(row, 19) if html_at(row, 19) else "",
            "Preferred qualifications:\n" + html_at(row, 18) if html_at(row, 18) else "",
            html_at(row, 3), html_at(row, 15)) if x)
        # The row's own URL (row[2]) is the sign-in-to-apply link; the
        # posting page by id is what a person opens and what the pipeline reads.
        out.append(Posting(id=str(row[0]), title=str(row[1]), url=f"{GOOGLE_SEARCH}{row[0]}",
                           location="; ".join(locs),
                           posted=_date(posted[0]) if isinstance(posted, list) and posted and isinstance(posted[0], (int, float)) else "",
                           text=text))
    return out


def google(watch: Watch, http: httpx.Client) -> list[Posting]:
    out = []
    for page in range(1, MAX_ROWS // 20 + 1):
        params = {"q": watch.query or "software engineer", "sort_by": "date", "page": page,
                  "target_level": watch.args.get("target_level", "EARLY")}
        if watch.args.get("location"):
            params["location"] = watch.args["location"]
        html = http.get(GOOGLE_SEARCH, params=params,
                        headers={"Referer": GOOGLE_SEARCH, "Accept": "text/html"}).raise_for_status().text
        rows = _google_rows(html)
        out.extend(rows)
        if len(rows) < 20:
            break
    return out


def amazon(watch: Watch, http: httpx.Client) -> list[Posting]:
    out = []
    for offset in range(0, MAX_ROWS, PAGE):
        params = {"base_query": watch.query or "software development engineer", "result_limit": PAGE,
                  "sort": "recent", "offset": offset}
        if watch.args.get("country"):
            params["country"] = watch.args["country"]
        data = http.get("https://www.amazon.jobs/en/search.json", params=params,
                        headers={"Accept": "application/json"}).raise_for_status().json()
        rows = data.get("jobs") or []
        for j in rows:
            text = "\n\n".join(strip_html(j.get(k, "")) for k in
                               ("description", "basic_qualifications", "preferred_qualifications") if j.get(k))
            out.append(Posting(id=str(j.get("id_icims") or j.get("job_path")), title=j.get("title", ""),
                               url=f"https://www.amazon.jobs{j.get('job_path')}" if j.get("job_path") else j.get("url_next_step", ""),
                               location=j.get("normalized_location") or j.get("location", ""),
                               posted=_date(j.get("posted_date", "")), text=text))
        if len(rows) < PAGE:
            break
    return out


APPLE_LINK = re.compile(r"/en-us/details/([0-9-]+)/([a-z0-9-]+)")


def apple(watch: Watch, http: httpx.Client) -> list[Posting]:
    out, seen = [], set()
    for page in range(1, 6):
        params = {"sort": "newest", "page": page}
        if watch.query:
            params["search"] = watch.query
        html = http.get("https://jobs.apple.com/en-us/search", params=params,
                        headers={"Accept": "text/html"}).raise_for_status().text
        found = 0
        for m in APPLE_LINK.finditer(html):
            job_id, slug = m.group(1), m.group(2)
            if job_id in seen:
                continue
            seen.add(job_id)
            found += 1
            title = " ".join(w.capitalize() for w in slug.split("-") if w)
            out.append(Posting(id=job_id, title=title, url=f"https://jobs.apple.com/en-us/details/{job_id}/{slug}"))
        if not found:
            break
    return out


PROVIDERS: dict[str, Callable[[Watch, httpx.Client], list[Posting]]] = {
    "greenhouse": greenhouse, "lever": lever, "ashby": ashby, "bamboohr": bamboohr, "workday": workday,
    "smartrecruiters": smartrecruiters, "oracle": oracle, "eightfold": eightfold,
    "google": google, "amazon": amazon, "apple": apple,
}


def fetch(watch: Watch, http: httpx.Client | None = None) -> list[Posting]:
    """Every current role on the watch's page, newest first. Raises."""
    provider = PROVIDERS.get(watch.provider)
    if provider is None:
        raise RuntimeError(f"unknown provider {watch.provider!r}")
    if http is not None:
        return provider(watch, http)
    with client() as http:
        return provider(watch, http)


# ------------------------------------------------------------- discover --
# A company's own careers page is usually a shell around one of the
# boards above: Mujin's `mujin-corp.com/careers` embeds a Lever board
# (Japan) and a BambooHR one (US). When the host is not a board, the
# page is read once and every board link in it is a watch.
BOARD_LINKS = [
    re.compile(r"https?://(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_board\?for=)?([A-Za-z0-9_-]+)", re.I),
    re.compile(r"https?://jobs\.lever\.co/([A-Za-z0-9_-]+)", re.I),
    re.compile(r"https?://jobs\.ashbyhq\.com/([A-Za-z0-9_-]+)", re.I),
    re.compile(r"https?://([A-Za-z0-9-]+)\.bamboohr\.com/(?:careers|js/embed)", re.I),
    re.compile(r"https?://[A-Za-z0-9-]+\.wd\d+\.myworkdayjobs\.com/[A-Za-z0-9_/-]+", re.I),
    re.compile(r"https?://(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9_-]+)", re.I),
]
BOARD_URL = {
    0: "https://boards.greenhouse.io/{}", 1: "https://jobs.lever.co/{}", 2: "https://jobs.ashbyhq.com/{}",
    3: "https://{}.bamboohr.com/careers", 5: "https://careers.smartrecruiters.com/{}",
}
EMBED_ONLY = {"embed", "js"}       # matches that are the script, not a board


def discover(url: str, http: httpx.Client | None = None) -> list[str]:
    """Board URLs found in the page at `url`, in page order, each once.
    Empty when the page names none. Raises when the page cannot be read."""
    def read(h: httpx.Client) -> str:
        return h.get(url).raise_for_status().text
    html = read(http) if http is not None else None
    if html is None:
        with client() as h:
            html = read(h)
    html = unescape(html)
    found: list[str] = []
    for i, pattern in enumerate(BOARD_LINKS):
        for m in pattern.finditer(html):
            if i in BOARD_URL:
                slug = m.group(1)
                if slug.lower() in EMBED_ONLY:
                    continue
                board = BOARD_URL[i].format(slug)
            else:
                board = m.group(0).rstrip("/")
            if board not in found:
                found.append(board)
    return found
