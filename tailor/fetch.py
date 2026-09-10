"""Posting scraper.

Pulls a job page down over plain HTTP and reduces it to readable text. This
handles server-rendered postings, which covers Greenhouse, Lever, Ashby, and
most company career pages. Postings behind a JavaScript render or a login are
left to the browser layer, which already has a real Chrome profile; a caller
that gets `JavaScriptRequired` should fall back to it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Optional

import httpx

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
TIMEOUT = 30.0
MIN_USEFUL_CHARS = 400
SKIP_TAGS = {"script", "style", "noscript", "svg", "head"}
BLOCK_TAGS = {
    "p", "div", "section", "article", "br", "li", "ul", "ol", "tr",
    "h1", "h2", "h3", "h4", "h5", "h6",
}


class JavaScriptRequired(RuntimeError):
    """The page returned too little text to be a real posting."""


@dataclass
class Posting:
    url: str
    text: str
    title: str = ""
    company: str = ""
    location: str = ""

    def to_markdown(self) -> str:
        header = [f"# {self.title or 'Job posting'}"]
        if self.company:
            header.append(f"**Company:** {self.company}")
        if self.location:
            header.append(f"**Location:** {self.location}")
        header.append(f"**Source:** {self.url}")
        return "\n\n".join(header) + "\n\n---\n\n" + self.text + "\n"


class _TextExtractor(HTMLParser):
    """Flattens HTML to text, keeping block-level line breaks."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0
        self.ld_json: list[str] = []
        self._in_ld = False

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            if dict(attrs).get("type") == "application/ld+json":
                self._in_ld = True
            self._skip_depth += 1
        elif tag in SKIP_TAGS:
            self._skip_depth += 1
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")
        elif tag == "li":
            self.parts.append("\n- ")

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS or tag == "script":
            self._skip_depth = max(0, self._skip_depth - 1)
            self._in_ld = False
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_ld:
            self.ld_json.append(data)
        elif self._skip_depth == 0 and data.strip():
            self.parts.append(data)

    def text(self) -> str:
        raw = "".join(self.parts)
        raw = re.sub(r"[ \t]+", " ", raw)
        raw = re.sub(r"\n\s*\n\s*\n+", "\n\n", raw)
        return "\n".join(line.strip() for line in raw.splitlines()).strip()


def _job_posting_ld(blobs: list[str]) -> Optional[dict]:
    """Find a schema.org JobPosting among the page's JSON-LD blocks."""
    for blob in blobs:
        try:
            parsed = json.loads(blob)
        except json.JSONDecodeError:
            continue
        candidates = parsed if isinstance(parsed, list) else [parsed]
        candidates += parsed.get("@graph", []) if isinstance(parsed, dict) else []
        for item in candidates:
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                return item
    return None


def _strip_html(value: str) -> str:
    extractor = _TextExtractor()
    extractor.feed(value)
    return extractor.text()


def fetch(url: str) -> Posting:
    with httpx.Client(
        follow_redirects=True,
        timeout=TIMEOUT,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
    ) as client:
        response = client.get(url)
        response.raise_for_status()
        html = response.text

    extractor = _TextExtractor()
    extractor.feed(html)
    text = extractor.text()

    posting = Posting(url=url, text=text)

    # Structured data beats the flattened page whenever the site provides it.
    ld = _job_posting_ld(extractor.ld_json)
    if ld:
        posting.title = str(ld.get("title") or "")
        org = ld.get("hiringOrganization")
        if isinstance(org, dict):
            posting.company = str(org.get("name") or "")
        location = ld.get("jobLocation")
        if isinstance(location, list) and location:
            location = location[0]
        if isinstance(location, dict):
            address = location.get("address")
            if isinstance(address, dict):
                posting.location = ", ".join(
                    str(address[key])
                    for key in ("addressLocality", "addressRegion", "addressCountry")
                    if address.get(key)
                )
        description = ld.get("description")
        if isinstance(description, str) and len(_strip_html(description)) > len(text):
            posting.text = _strip_html(description)

    if not posting.title:
        match = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
        posting.title = match.group(1).strip() if match else ""

    if len(posting.text) < MIN_USEFUL_CHARS:
        raise JavaScriptRequired(
            f"{url} returned {len(posting.text)} chars of text; "
            "the posting likely renders client-side or needs a login"
        )

    return posting
