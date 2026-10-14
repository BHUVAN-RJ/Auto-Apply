"""Does this place look like it is not in the United States?

`scout.filter.outside_us` answers the easy half: a location that names
another *country* ("Bengaluru, India", "London, United Kingdom"). The
first real crawl showed what it misses, because a board writes the city
and stops: "Bucharest", "Kuala Lumpur", "Manchester", "Vladivostok",
"South America" all arrived at the top of the list as roles to read.

Three rules, in this order, and the order is the whole design:

1. **A US signal wins.** A state name or code, "United States", a bare
   "Remote". This runs first on purpose: "Manchester, NH" and "Vancouver,
   WA" are American, and a city list that ran first would throw them away.
2. **Then a foreign city or region.** The list below is the places a
   software job board actually writes, and it is deliberately short of
   anything the first rule cannot rescue.
3. **Otherwise, keep it.** "San Francisco" with no state is the common
   shape and must survive, so an unknown place is American until something
   says otherwise.

And nothing here drops a role. The crawl puts `place/foreign` on it and
the page hides those by default with the count showing, because a filter
written from one morning's reading will be wrong about somewhere, and a
role dropped before it is stored cannot be found again to prove it.
"""
from __future__ import annotations

import re

from scout import filter as level_filter
from tailor.screen import US_STATE_CODES, US_STATE_NAMES

# What the crawl writes onto a role whose place reads as abroad.
FLAG = "place/foreign"

# Enough of a US signal to settle the question on its own.
US_WORDS = (
    "united states", "usa", "u.s.", "u.s.a", "us-remote", "remote - us",
    "us remote", "remote (us)", "remote, us", "anywhere in the us",
    "puerto rico", "washington dc", "washington, d.c.", "district of columbia",
)

# Cities and regions a job board writes without its country. Ambiguous
# names are here on purpose - Manchester, Vancouver, Birmingham, London and
# Hamilton all exist in America too - because rule 1 has already kept the
# American ones by their state; what reaches this list has no state on it,
# and on a software board that is far more often the larger city abroad.
FOREIGN_PLACES = (
    "london", "manchester", "edinburgh", "glasgow", "birmingham", "bristol",
    "cambridge, uk", "dublin", "belfast", "amsterdam", "rotterdam", "utrecht",
    "berlin", "munich", "hamburg", "frankfurt", "cologne", "stuttgart",
    "paris", "lyon", "toulouse", "madrid", "barcelona", "valencia", "lisbon",
    "porto", "milan", "rome", "turin", "zurich", "geneva", "basel", "vienna",
    "brussels", "luxembourg", "copenhagen", "stockholm", "oslo", "helsinki",
    "reykjavik", "warsaw", "krakow", "wroclaw", "prague", "brno", "budapest",
    "bucharest", "cluj", "sofia", "belgrade", "zagreb", "athens", "istanbul",
    "ankara", "kyiv", "kiev", "lviv", "moscow", "saint petersburg",
    "vladivostok", "novosibirsk", "minsk", "riga", "vilnius", "tallinn",
    "tel aviv", "haifa", "jerusalem", "dubai", "abu dhabi", "doha", "riyadh",
    "cairo", "nairobi", "lagos", "accra", "johannesburg", "cape town",
    "bengaluru", "bangalore", "mumbai", "new delhi", "gurgaon", "gurugram",
    "noida", "hyderabad", "pune", "chennai", "kolkata", "ahmedabad",
    "colombo", "dhaka", "karachi", "lahore", "islamabad", "kathmandu",
    "singapore", "kuala lumpur", "jakarta", "bangkok", "manila", "cebu",
    "ho chi minh", "hanoi", "phnom penh", "taipei", "hong kong", "shanghai",
    "beijing", "shenzhen", "guangzhou", "hangzhou", "seoul", "busan",
    "tokyo", "osaka", "kyoto", "yokohama", "fukuoka", "sydney", "melbourne",
    "brisbane", "perth", "adelaide", "canberra", "auckland", "wellington",
    "christchurch", "toronto", "vancouver", "montreal", "montréal", "ottawa",
    "calgary", "edmonton", "winnipeg", "waterloo, on", "quebec", "halifax",
    "mexico city", "guadalajara", "monterrey", "san josé, costa rica",
    "san jose, costa rica", "panama city", "bogota", "bogotá", "medellin",
    "medellín", "lima", "santiago", "buenos aires", "montevideo",
    "são paulo", "sao paulo", "rio de janeiro", "belo horizonte",
    "latam", "latin america", "south america", "emea", "apac", "anz",
    "middle east", "africa", "europe", "asia", "eu remote", "uk remote",
    "remote - emea", "remote - latam", "remote (emea)", "remote - europe",
    "remote - india", "remote - canada", "canada", "india",
)


def _word(needle: str, hay: str) -> bool:
    return re.search(r"(?<![a-z])" + re.escape(needle) + r"(?![a-z])", hay) is not None


def names_us(place: str) -> bool:
    """A state, a US phrase, or nothing but "remote" - any of which settles
    it before a city is looked at."""
    low = " " + place.lower() + " "
    if any(w in low for w in US_WORDS):
        return True
    if any(_word(name, low) for name in US_STATE_NAMES):
        return True
    if re.search(r"(?<![A-Za-z])(" + "|".join(US_STATE_CODES) + r")(?![A-Za-z])", place):
        return True
    return low.strip() in ("remote", "remote - us", "us")


def looks_foreign(place: str) -> bool:
    """Rules 1 to 3, in order. An empty place is not foreign: plenty of
    boards simply do not say, and the posting's own text is what the screen
    reads."""
    if not place or not place.strip():
        return False
    if names_us(place):
        return False
    if level_filter.outside_us(place):
        return True
    low = place.lower()
    return any(_word(name, " " + low + " ") if " " not in name else name in low
               for name in FOREIGN_PLACES)
