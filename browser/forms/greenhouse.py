"""Greenhouse (`job-boards.greenhouse.io`, `boards.greenhouse.io`, or embedded
on a careers page with `gh_jid` in the URL).

One page, one form, the most stable ids on the web: `first_name`,
`last_name`, `email`, `phone`, file inputs named `resume` and
`cover_letter`. Custom questions are `question_<id>` (new boards) or
`job_application_answers_attributes_<n>_...` (old). Dropdowns are
react-select comboboxes. The embedded form is a cross-origin iframe; the
engine navigates to its src when the host page shows no fields.
"""
from __future__ import annotations

from .engine import Adapter


class Greenhouse(Adapter):
    NAME = "greenhouse"
    SELECTORS = [
        (r"^first_name\||\|first_name\||job_application\[first_name\]", "first_name"),
        (r"^last_name\||\|last_name\||job_application\[last_name\]", "last_name"),
        (r"^email\||\|email\||job_application\[email\]", "email"),
        (r"^phone\||\|phone\||job_application\[phone\]", "phone"),
        (r"candidate-location|job_application\[location\]|^location\|", "location"),
    ]
    RESUME = r"^resume$|\bresume\b"
    COVER_LETTER = r"cover_letter|cover letter"
