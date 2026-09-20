"""How much of a page's text is the job posting, without asking a model.

Three copies of a posting can reach the pipeline: what the server fetched
from the employer's URL, what the browser saw on that page, and Jobright's
copy. The longest is not the best (a form page clears 400 characters on
cookie notices and field labels alone), so each is cleaned of the lines
every page has and scored on what a description is made of: words, the
section headings postings use, bullets. The same cleaned text feeds the
simhash the already-seen check compares postings with.
"""
from __future__ import annotations

import hashlib
import re

# Lines that are the site, not the posting.
BOILERPLATE = re.compile(
    r"cookie|privacy policy|terms of (use|service)|sign in|log in|sign up|create account|"
    r"apply now|apply for this job|share this job|refer a friend|back to jobs|all rights reserved|"
    r"©|powered by|accept all|manage preferences|skip to (main )?content|view all jobs|"
    r"autofill with|upload (your )?resume|first name|last name|phone number|linkedin profile|"
    r"submit application|required fields|equal opportunity",
    re.I,
)

# Section headings a description is built from. Each found is worth a bonus.
SECTIONS = re.compile(
    r"\b(responsibilities|requirements|qualifications|what you('| wi)ll do|what you('| wi)ll bring|"
    r"about (the|this) (role|position|team|job)|who you are|what we('re| are) looking for|"
    r"nice to have|preferred|benefits|compensation|salary|minimum qualifications|"
    r"basic qualifications|the role|your impact|day to day)\b",
    re.I,
)

BULLET = re.compile(r"^\s*[•\-\*·▪◦]\s+")
SECTION_BONUS = 40
BULLET_BONUS = 8
BONUS_CAP = 400

# Below this share of the best candidate's score, the employer's own page
# is not the posting, whatever it is.
GOOD_ENOUGH = 0.6

_WORD = re.compile(r"[a-z0-9]+")


def clean(text: str) -> str:
    """The lines of `text` that could be the posting: no boilerplate, no
    short labels, and each line once."""
    seen: set[str] = set()
    kept: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        key = line.lower()
        if key in seen:
            continue
        seen.add(key)
        if BOILERPLATE.search(line) and len(line.split()) < 12:
            continue
        if len(line.split()) < 4 and not SECTIONS.search(line):
            continue
        kept.append(line)
    return "\n".join(kept)


def score(text: str) -> int:
    """Words in the cleaned text, plus a capped bonus for section headings
    and bullets. Zero for nothing."""
    cleaned = clean(text)
    if not cleaned:
        return 0
    words = len(cleaned.split())
    sections = len(set(m.group(0).lower() for m in SECTIONS.finditer(cleaned)))
    bullets = sum(1 for line in cleaned.splitlines() if BULLET.match(line))
    bonus = min(BONUS_CAP, sections * SECTION_BONUS + bullets * BULLET_BONUS)
    return words + bonus


def best(candidates: list[tuple[str, str]]) -> tuple[str, str] | None:
    """(name, text) with the highest score; the first of equals wins, so put
    the authoritative copy first. None when every text scores zero."""
    scored = [(score(text), index, name, text) for index, (name, text) in enumerate(candidates)]
    scored = [item for item in scored if item[0] > 0]
    if not scored:
        return None
    top = max(scored, key=lambda item: (item[0], -item[1]))
    return top[2], top[3]


def simhash(text: str) -> int:
    """64-bit simhash over word shingles of the cleaned text. Two copies of
    one posting land within a few bits of each other."""
    words = _WORD.findall(clean(text).lower())
    if len(words) < 3:
        return 0
    shingles = [" ".join(words[i:i + 3]) for i in range(len(words) - 2)]
    weights = [0] * 64
    for shingle in shingles:
        digest = int.from_bytes(hashlib.blake2b(shingle.encode(), digest_size=8).digest(), "big")
        for bit in range(64):
            weights[bit] += 1 if digest >> bit & 1 else -1
    value = 0
    for bit, weight in enumerate(weights):
        if weight > 0:
            value |= 1 << bit
    return value


def distance(a: int, b: int) -> int:
    return bin(a ^ b).count("1")
