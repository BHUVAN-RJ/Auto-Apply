"""Ashby (`jobs.ashbyhq.com/<org>/<job>/application`).

One page under the "Application" tab. System fields carry
`_systemfield_<name>` names; the name field is one box for the full name.
Location is a free-text autocomplete. Custom questions are labelled
plainly. Each file field has its own hidden `<input type=file>`.
"""
from __future__ import annotations

import re

from .engine import Adapter


class Ashby(Adapter):
    NAME = "ashby"
    SELECTORS = [
        (r"_systemfield_name\b", "full_name"),
        (r"_systemfield_email", "email"),
        (r"_systemfield_phone", "phone"),
        (r"_systemfield_location", "location"),
        (r"_systemfield_linkedin", "linkedin"),
        (r"_systemfield_github", "github"),
        (r"_systemfield_website|_systemfield_portfolio", "website"),
    ]
    RESUME = r"_systemfield_resume|resume"
    COVER_LETTER = r"cover"

    def apply_url(self, url: str) -> str:
        # The posting page has no form; the application page does.
        m = re.match(r"(https?://jobs\.ashbyhq\.com/[^/?#]+/[0-9a-f-]{36})", url or "", re.I)
        if m:
            return m.group(1) + "/application"
        return url
