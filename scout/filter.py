"""Title filter: at the applicant's level or not. Substring matches on
the lowercased title, so `engineer ii` matches "Software Engineer II,
YouTube" and `senior` drops "Senior Software Engineer". Word-bounded,
so `iv` does not fire inside "Driver"."""
from __future__ import annotations

import re

from scout import Posting, Watch


def _hit(needle: str, title: str) -> bool:
    needle = needle.strip().lower()
    if not needle:
        return False
    # Word boundaries around the phrase; "sr." keeps its dot, "sr " its space.
    pattern = r"(?<![a-z0-9])" + re.escape(needle) + r"(?![a-z0-9])"
    return re.search(pattern, title) is not None


def at_level(title: str, positive: list[str], negative: list[str]) -> bool:
    title = (title or "").lower()
    if any(_hit(n, title) for n in negative):
        return False
    return not positive or any(_hit(p, title) for p in positive)


def matching(watch: Watch, postings: list[Posting]) -> list[Posting]:
    return [p for p in postings if at_level(p.title, watch.positive, watch.negative)]
