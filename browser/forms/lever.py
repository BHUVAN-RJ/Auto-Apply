"""Lever (`jobs.lever.co/<org>/<job>/apply`).

Plain HTML form, fields by `name`: `name` (full), `email`, `phone`, `org`
(current company), `urls[LinkedIn]`, `urls[GitHub]`, `urls[Portfolio]`,
`urls[Twitter]`, `location` (autocomplete), `resume` (file), `comments`,
custom cards as `cards[<id>][field<n>]`, EEO as `eeo[gender]` and so on.
The posting page is `/<job>`; the form is `/<job>/apply`.
"""
from __future__ import annotations

import re

from .engine import Adapter


class Lever(Adapter):
    NAME = "lever"
    SELECTORS = [
        (r"\|name\|", "full_name"),
        (r"\|email\|", "email"),
        (r"\|phone\|", "phone"),
        (r"\|org\|", "current_company"),
        (r"urls\[linkedin\]", "linkedin"),
        (r"urls\[github\]", "github"),
        (r"urls\[(portfolio|website)\]", "website"),
        (r"urls\[twitter\]", "twitter"),
        (r"\|location\|", "location"),
        (r"eeo\[gender\]", "gender"),
        (r"eeo\[race\]", "race"),
        (r"eeo\[veteran\]", "veteran"),
        (r"eeo\[disability\]", "disability"),
    ]
    RESUME = r"\bresume\b"
    COVER_LETTER = r"cover"
    # A text box with Lever's own suggestion list under it; a typed value
    # that is not picked from the list is dropped on blur.
    COMBOBOX = r"\|location\|"

    def apply_url(self, url: str) -> str:
        m = re.match(r"(https?://jobs\.(?:eu\.)?lever\.co/[^/?#]+/[0-9a-f-]{36})(/apply)?", url or "", re.I)
        if m:
            return m.group(1) + "/apply"
        return url
