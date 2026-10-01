"""LinkedIn, which hides the posting from a fetch and obfuscates it from a scrape.

Two facts decide everything here, both measured on a real posting
(2026-09-30, `linkedin.com/jobs/view/4470028949`):

1. **A plain fetch of the job page is mostly LinkedIn.** It comes back with
   1778 words, of which the description is 194: the rest is navigation,
   "Expand search", cookie text and a sign-in pitch. The tailor reads a
   ranked list of the posting's own terms, and this is a page about LinkedIn.
2. **The page's own class names are generated and rotate** (`bghmnt bghmns
   bghr4 bgha1f …` on the apply button), so nothing may be selected by class.
   Anchors that hold still: `aria-label`, visible text, `document.title`
   ("Role | Company | LinkedIn"), and the job id in the URL.

But LinkedIn publishes the posting itself, without a login and without a key,
for its own share links:

    GET linkedin.com/jobs-guest/jobs/api/jobPosting/<id>

which answers the description, the title, the company and the place as plain
HTML. So the posting never needs the browser at all - only the application
does. That is the division this module keeps: the text comes from the guest
endpoint, and the signed-in page is left for the Easy Apply flow.
"""

from __future__ import annotations

import html
import re
from typing import Optional

import httpx

from .fetch import Posting

GUEST = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{id}"
TIMEOUT = 20.0
# The guest endpoint answers a browser, not a script with no user agent.
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# `/jobs/view/<id>`, and the id a search or a collection page carries in its
# query while the path says something else entirely.
_VIEW = re.compile(r"/jobs/view/(\d+)")
_QUERY = re.compile(r"[?&]currentJobId=(\d+)")

_DESCRIPTION = re.compile(
    r'class="[^"]*show-more-less-html__markup[^"]*"[^>]*>(.*?)</div>', re.S)
_TITLE = re.compile(r'topcard__title[^>]*>\s*([^<]+)')
_COMPANY = re.compile(r'topcard__org-name-link[^>]*>\s*([^<]+)')
_PLACE = re.compile(r'topcard__flavor--bullet[^>]*>\s*([^<]+)')
# "Seniority level / Entry level", "Employment type / Full-time". The
# subheader and its value are siblings with a little markup between them.
_CRITERIA = re.compile(
    r'job-criteria-subheader[^>]*>\s*([^<]+?)\s*</h3>.{0,200}?'
    r'job-criteria-text[^>]*>\s*([^<]+?)\s*<', re.S)


def job_id(url: str) -> Optional[str]:
    """The LinkedIn job id in this URL, or None if it is not one."""
    if "linkedin.com" not in (url or "").lower():
        return None
    found = _VIEW.search(url) or _QUERY.search(url)
    return found.group(1) if found else None


def _plain(fragment: str) -> str:
    """The text of an HTML fragment, with its line breaks kept."""
    text = re.sub(r"<(br|/p|/li|/h\d)[^>]*>", "\n", fragment, flags=re.I)
    text = re.sub(r"<li[^>]*>", "- ", text, flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    text = "\n".join(line.strip() for line in text.splitlines())
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def parse(body: str, url: str) -> Optional[Posting]:
    """A `Posting` out of the guest endpoint's HTML, or None without one."""
    found = _DESCRIPTION.search(body)
    if not found:
        return None
    description = _plain(found.group(1))
    if not description:
        return None
    head = [html.unescape(m.group(1).strip())
            for m in (_TITLE.search(body), _COMPANY.search(body), _PLACE.search(body)) if m]
    title = head[0] if len(head) > 0 else ""
    company = head[1] if len(head) > 1 else ""
    place = head[2] if len(head) > 2 else ""
    # "Seniority level: Entry level", "Employment type: Full-time" and the
    # rest: short, and the screen reads exactly these words.
    criteria = [f"{html.unescape(k.strip())}: {html.unescape(v.strip())}"
                for k, v in _CRITERIA.findall(body)]
    text = "\n".join(filter(None, [
        title, company, place, "", description,
        "\n".join(criteria) if criteria else "",
    ])).strip()
    return Posting(url=url, text=text, title=title, company=company, location=place)


def posting(url: str) -> Optional[Posting]:
    """The posting behind a LinkedIn job URL, or None when it is not one.

    Raises nothing the caller has to handle: a LinkedIn URL this cannot read
    falls back to the ordinary fetch, which is what happened before.
    """
    jid = job_id(url)
    if not jid:
        return None
    try:
        with httpx.Client(follow_redirects=True, timeout=TIMEOUT,
                          headers={"User-Agent": USER_AGENT}) as client:
            response = client.get(GUEST.format(id=jid))
            response.raise_for_status()
            return parse(response.text, url)
    except Exception:  # noqa: BLE001 - the ordinary fetch is the fallback
        return None
