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


def check_click(**fields: str | None) -> None:
    """Raise if this element must not be clicked."""
    if describes_submit(*fields.values()):
        described = ", ".join(f"{k}={v!r}" for k, v in fields.items() if v)
        raise SubmitBlocked(
            f"refusing to click a submit control ({described}). "
            "Auto-Apply never submits; the human does."
        )
