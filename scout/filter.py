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
    return [p for p in postings if at_level(p.title, watch.positive, watch.negative)
            and not (watch.us_only and outside_us(p.location))]


# ------------------------------------------------------------------ place
# "US only" in code, on the listing's location line, before the screen.
# Only a location that names another country or one of its cities is
# out; an empty line, "Remote", a US state or city, or anything the
# lists do not know stays in, and the screen reads the posting.
NON_US = [
    "japan", "tokyo", "osaka", "aichi", "okazaki", "netherlands", "best,", "amsterdam", "eindhoven",
    "united kingdom", "uk", "london", "england", "scotland", "ireland", "dublin", "germany", "berlin",
    "munich", "france", "paris", "spain", "madrid", "barcelona", "italy", "milan", "rome", "poland",
    "warsaw", "sweden", "stockholm", "norway", "denmark", "copenhagen", "finland", "helsinki",
    "switzerland", "zurich", "austria", "vienna", "belgium", "brussels", "portugal", "lisbon",
    "czech", "prague", "hungary", "romania", "israel", "tel aviv", "india", "bangalore", "bengaluru",
    "hyderabad", "chennai", "pune", "mumbai", "delhi", "gurgaon", "gurugram", "noida", "china",
    "beijing", "shanghai", "shenzhen", "hong kong", "taiwan", "taipei", "korea", "seoul", "singapore",
    "australia", "sydney", "melbourne", "new zealand", "auckland", "canada", "toronto", "vancouver",
    "montreal", "ottawa", "waterloo", "mexico", "brazil", "sao paulo", "são paulo", "argentina",
    "colombia", "chile", "philippines", "manila", "vietnam", "indonesia", "jakarta", "malaysia",
    "thailand", "bangkok", "uae", "dubai", "abu dhabi", "saudi", "egypt", "nigeria", "kenya",
    "south africa", "turkey", "istanbul", "ukraine", "kyiv", "russia", "moscow", "emea", "apac", "latam",
]
US_WORDS = ["united states", "usa", "u.s.", "us-", "remote - us", "us remote"]


def _part_outside(loc: str) -> bool:
    if not loc.strip() or any(w in loc for w in US_WORDS):
        return False
    return any(_hit(n.rstrip(","), loc) if not n.endswith(",") else n in loc for n in NON_US)


def outside_us(location: str) -> bool:
    """Every listed place is abroad. "Austin, TX; Toronto, Canada" stays:
    one of them is here."""
    loc = (location or "").lower()
    parts = [x for x in re.split(r"[;|/]|\bor\b", loc) if x.strip()]
    return bool(parts) and all(_part_outside(x) for x in parts)


# ---------------------------------------------------------------- prescreen
# Hard rejects read off the description in code, before the model sees
# it. In bulk most of what the title filter lets through is still not
# entry level ("5+ years of experience" under a plain "Software Engineer"
# title), and each of those is a model call and a wait. Only the cheap,
# high-precision reasons live here; anything softer is the model's.
YEARS_MIN = 4          # "4+ years", "4-6 years", "at least 4 years" and up is not entry level
_YEARS = re.compile(
    r"(?:(?:minimum|at least|min\.?)\s+(?:of\s+)?)?(\d{1,2})\s*(?:\+|\s*(?:-|–|to)\s*\d{1,2})?\s*"
    r"(?:\+\s*)?years?(?:'|’)?\s+(?:of\s+)?(?:\w+\s+){0,4}?experience", re.I)
_CLEARANCE = re.compile(
    r"(?:active|current|hold(?:s|ing)?\s+an?|must\s+(?:have|hold|possess|obtain)\s+an?|requires?\s+an?|"
    r"eligible\s+(?:for|to\s+obtain)\s+an?)\s+(?:(?:top\s+secret|secret|ts/?sci|dod|government|security|public\s+trust)\s+)+clearance",
    re.I)
_CITIZEN = re.compile(
    r"(?:u\.?s\.?|united\s+states)\s+citizen(?:ship)?\s+(?:is\s+)?(?:required|is\s+a\s+requirement|only)|"
    r"must\s+be\s+an?\s+(?:u\.?s\.?|united\s+states)\s+citizen|(?:u\.?s\.?|united\s+states)\s+citizens\s+only",
    re.I)


def prescreen(text: str) -> str | None:
    """Why the posting is out before a model reads it, else None. Years
    of experience at `YEARS_MIN` or more, a security clearance, or
    citizenship as a requirement. "0-2 years" and "2+ years" pass: the
    first number of a range is what the role really asks."""
    text = text or ""
    if not text.strip():
        return None
    years = [int(m.group(1)) for m in _YEARS.finditer(text)]
    years = [y for y in years if y <= 30]      # "2024 years" is a date that slipped in
    if years and min(years) >= YEARS_MIN:
        return f"asks for {min(years)}+ years of experience"
    if _CLEARANCE.search(text):
        return "needs a security clearance"
    if _CITIZEN.search(text):
        return "US citizenship required"
    return None
