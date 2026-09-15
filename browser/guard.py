"""The never-submit guarantee.

The agent fills forms and stops. Submission is always a human click. That rule
is enforced here, in the action layer, rather than in the prompt: a prompt rule
is a request the model may misread under pressure, while a refused action
cannot happen at all.

Two independent defences, because either alone has a failure mode:

1. A deny-list on what the agent is allowed to click. Element text, value,
   aria-label, id, and name are all checked.
2. Removal of the actions that could submit without a click at all — running
   arbitrary JavaScript, or pressing Enter inside a form.

A third rule, same mechanism, different reason: the agent never answers a
question about visa, sponsorship, or work authorisation. An invented answer
there is not a typo, it is a misrepresentation on an immigration matter.
Jobright's autofill may set those from the applicant's own profile; the
agent leaves whatever it finds and does not touch the field.
"""

from __future__ import annotations

import re

# Matched against element text and attributes, case-insensitively, on word
# boundaries so that "Submit" is caught but "Resubmitted elsewhere" in a
# paragraph is not the deciding factor for a button that reads "Back".
SUBMIT_PATTERNS = [
    r"\bsubmit\b",
    r"\bapply\s*(now|for|to)?\b",
    r"\bsend\s*(application|now)?\b",
    r"\bfinish\b",
    r"\bcomplete\s*application\b",
    r"\bconfirm\s*(and)?\s*(send|submit|apply)\b",
    r"\bsubmit\s*application\b",
]

# Actions removed from the agent entirely. `evaluate` runs arbitrary
# JavaScript, which would make every other guard here decorative;
# `send_keys` can press Enter, which submits a single-input form without
# any button being clicked.
FORBIDDEN_ACTIONS = ("evaluate", "send_keys")

_COMPILED = [re.compile(pattern, re.I) for pattern in SUBMIT_PATTERNS]

# Checked before the deny-list. A "Save and continue" on a multi-step form is
# navigation, not submission, and a "Submit" that is plainly a search button
# should not block the run either.
ALLOW_PATTERNS = [
    r"\bsave\s*(and)?\s*(continue|draft|progress)\b",
    r"\bnext\s*(step|page)?\b",
    r"\bcontinue\s*(to)?\b",
    r"\bsearch\b",
    r"\bupload\b",
    r"\bapply\s*filters?\b",
]
_ALLOWED = [re.compile(pattern, re.I) for pattern in ALLOW_PATTERNS]


class SubmitBlocked(RuntimeError):
    """The agent tried to click something that looks like a submit control."""


class ProtectedField(RuntimeError):
    """The agent tried to answer a visa or work-authorisation question."""


# Questions the agent must leave alone, matched against the field's label,
# accessible name, id, and name. Broad on purpose: a false positive here costs
# one blank field the human fills in; a false negative is a wrong answer about
# immigration status under the applicant's name.
PROTECTED_PATTERNS = [
    r"\bvisas?\b",
    r"\bsponsor(ship|ed)?\b",
    r"\bwork\s*(authori[sz]ation|authori[sz]ed|permit|status|eligib\w*)\b",
    r"\b(authori[sz]ed|eligible|entitled|legally\s*able|right)\s+to\s+work\b",
    r"\bemployment\s*(authori[sz]ation|eligibility|visa)\b",
    r"\bimmigration\b",
    r"\bcitizen(ship)?\b",
    r"\bnational(ity)?\b",
    r"\b(green\s*card|permanent\s*resident\w*|lawful)\b",
    # "opt" the visa, not "opt in" / "opt out" on a mailing-list checkbox.
    r"\b(opt(?!\s*(in|out)\b)|cpt|stem\s*opt|ead|h-?1-?b|h1b|f-?1|j-?1|l-?1|tn|o-?1|e-?3)\b",
    r"\bi-?9\b",
]
_PROTECTED = [re.compile(pattern, re.I) for pattern in PROTECTED_PATTERNS]


def describes_submit(*fields: str | None) -> bool:
    """True when any field reads like a submit control.

    Every field is considered: a button whose visible text is an icon still
    usually carries `aria-label="Submit application"` or `id="submit-btn"`.
    """
    for field in fields:
        if not field:
            continue
        # Attribute values glue words with underscores, hyphens, and dots
        # ("submit_application", "btn-submit"), where \b finds no boundary.
        # Splitting on those first makes attributes match like visible text.
        text = " ".join(re.sub(r"[_\-./]+", " ", str(field)).split())
        if not text or len(text) > 200:
            # Very long text is a paragraph, not a control label; judging a
            # button by prose that happens to contain "apply" is a false
            # positive waiting to happen.
            continue
        if any(pattern.search(text) for pattern in _ALLOWED):
            continue
        if any(pattern.search(text) for pattern in _COMPILED):
            return True
    return False


def describes_protected(*fields: str | None) -> bool:
    """True when any field reads like a visa or work-authorisation question."""
    for field in fields:
        if not field:
            continue
        text = " ".join(re.sub(r"[_\-./]+", " ", str(field)).split())
        # Long text is a question or paragraph, not a control label — but a
        # visa question *is* long, and the input's accessible name carries
        # it in full, so the limit is looser here than for submit.
        if not text or len(text) > 400:
            continue
        if any(pattern.search(text) for pattern in _PROTECTED):
            return True
    return False


COVER_LETTER_PATTERNS = [r"\bcover\s*letter\b", r"\bcover\b", r"\bletter\b"]
_COVER_LETTER = [re.compile(pattern, re.I) for pattern in COVER_LETTER_PATTERNS]


def describes_cover_letter(*fields: str | None) -> bool:
    """True when a file input belongs to a cover letter rather than a resume."""
    for field in fields:
        if not field:
            continue
        text = " ".join(re.sub(r"[_\-./]+", " ", str(field)).split())
        if text and len(text) <= 200 and any(p.search(text) for p in _COVER_LETTER):
            return True
    return False


def _describe(fields: dict) -> str:
    return ", ".join(f"{k}={v!r}" for k, v in fields.items() if v)


def check_click(**fields: str | None) -> None:
    """Raise if this element must not be clicked."""
    if describes_submit(*fields.values()):
        raise SubmitBlocked(
            f"refusing to click a submit control ({_describe(fields)}). "
            "Auto-Apply never submits; the human does."
        )
    check_protected(**fields)


def check_protected(**fields: str | None) -> None:
    """Raise if this element belongs to a visa or work-authorisation question."""
    if describes_protected(*fields.values()):
        raise ProtectedField(
            f"refusing to touch a visa / work-authorisation field ({_describe(fields)}). "
            "Leave it as it is, even if empty, and mention it when you call done."
        )
