"""What may be tracked, and what may never be.

The tracker records characters, not shapes: which value was corrected to what
is half of what it exists to answer, and `tools/dev_workday_recorder.py`
cannot say it because it keeps lengths. That is a decision with two edges, so
both are written down here rather than left in the collector, where a change
to the drain would quietly change what lands on disk.

Three rules, in order of how badly they matter:

1. **A secret is never recorded, not even its length.** A password, a
   one-time code, an SSN, a card number. Decided by the input's type and by
   the words around it, and when the question is open the answer is "secret":
   a field this cannot read is treated as one.
2. **Some pages are not tracked at all.** The browser this watches holds the
   person's mail. Mail, banking and password managers are off, by host, and a
   page that is off is not read - no keys, no snapshot, no URL beyond its
   bare host.
3. **The person's own details are scrubbed from the pages that are saved.**
   The keystroke log is theirs and keeps what they typed; the saved HTML is a
   fixture we may want to keep, and a fixture carrying a name has been
   committed to a public repository once already (2026-10-11).

Nothing here touches a browser or a file, so it can be read and tested on
its own.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

# --- 2. where we do not look ---------------------------------------------

# Mail, banking, payment and password managers. Matched on the host, and a
# subdomain of a named host counts. This is not a guess at what is private:
# it is the list of places where a keystroke is a credential or a letter.
OFF_LIMITS = (
    # mail and messaging
    "mail.google.com", "inbox.google.com", "outlook.com", "outlook.office.com",
    "outlook.live.com", "mail.yahoo.com", "mail.proton.me", "protonmail.com",
    "icloud.com", "messages.google.com", "web.whatsapp.com", "messenger.com",
    "web.telegram.org", "slack.com", "discord.com",
    # money
    "chase.com", "bankofamerica.com", "wellsfargo.com", "citi.com",
    "capitalone.com", "americanexpress.com", "discover.com", "usbank.com",
    "schwab.com", "fidelity.com", "vanguard.com", "coinbase.com", "robinhood.com",
    "paypal.com", "venmo.com", "wise.com", "stripe.com", "revolut.com",
    "irs.gov", "ssa.gov",
    # the keys themselves
    "account.proton.me", "pass.proton.me", "1password.com", "lastpass.com",
    "bitwarden.com", "vault.bitwarden.com", "dashlane.com", "keepersecurity.com",
    "accounts.google.com", "login.microsoftonline.com", "appleid.apple.com",
    "myaccount.google.com", "passwords.google.com",
    # health
    "mychart.com", "mychart.org",
)

# Not a page at all, or ours. The review page is the app talking to itself.
NOT_A_PAGE = ("about:", "chrome:", "chrome-extension:", "devtools:", "edge:",
              "view-source:", "data:", "blob:")


def host_of(url: str) -> str:
    return (urlsplit(url or "").hostname or "").lower()


def off_limits(url: str) -> bool:
    host = host_of(url)
    if not host:
        return False
    return any(host == h or host.endswith("." + h) for h in OFF_LIMITS)


def our_own(url: str, server_origin: str = "") -> bool:
    """The app's own page. Tracked, but it is us, so it is labelled."""
    host = host_of(url)
    if host in ("127.0.0.1", "localhost"):
        return True
    return bool(server_origin) and (url or "").startswith(server_origin)


def trackable(url: str) -> bool:
    """Whether this page may be read at all.

    A blank or internal tab is not refused: *that it opened* is the single
    most-asked question this tool has, and answering it needs no reading of
    the page. `readable()` is the stricter question.
    """
    if not url:
        return True
    if any((url or "").lower().startswith(p) for p in NOT_A_PAGE):
        return True
    return not off_limits(url)


def readable(url: str) -> bool:
    """Whether the page's contents - its keys, its fields, its HTML - may be
    read. A blank tab has nothing to read; an off-limits page must not be."""
    if not url:
        return False
    if any((url or "").lower().startswith(p) for p in NOT_A_PAGE):
        return False
    return not off_limits(url)


def why_not(url: str) -> str:
    """The reason a page is only counted and never read, for the log."""
    if not url:
        return "blank"
    if any((url or "").lower().startswith(p) for p in NOT_A_PAGE):
        return "not a web page"
    if off_limits(url):
        return "off limits by host"
    return ""


# --- 1. what is never typed into the log ---------------------------------

# The input types whose value is a credential whatever it is labelled.
SECRET_TYPES = ("password",)

# The words that make a field a secret. Deliberately wide: a field this
# cannot read is treated as a secret, so a miss costs a log line and never a
# credential. `code` alone would catch "zip code" and "area code", so the
# one-time sense is spelled out.
SECRET_WORDS = re.compile(
    r"\bpass(word|phrase|code)\b|\bpwd\b|\bsecret\b|\bprivate\s*key\b|"
    r"\b(one[\s-]?time|verification|security|auth(entication)?|sms|2fa|mfa|otp)\s*code\b|"
    r"\botp\b|\bmfa\b|\b2fa\b|\btotp\b|\bpin\b|\btoken\b|\bapi\s*key\b|"
    r"\bssn\b|\bsocial\s*security\b|\btax\s*id\b|\bsin\b|"
    r"\bcvv\b|\bcvc\b|\b(card|security)\s*(number|code)\b|\bcredit\s*card\b|"
    r"\baccount\s*number\b|\brouting\b|\biban\b|\bsort\s*code\b|"
    r"\bdate\s*of\s*birth\b|\bdob\b|\bdriver'?s?\s*licen[cs]e\b|\bpassport\b",
    re.I)

# An autocomplete attribute the browser itself treats as a credential.
SECRET_AUTOCOMPLETE = re.compile(
    r"(current|new)-password|one-time-code|cc-(number|csc|exp)", re.I)


def words_in(*fields: str) -> str:
    """An identifier as words, so a phrase regex can read it.

    `verifyPassword` has no word boundary in front of "Password" and
    `api_key` has none after "api", so the patterns matched neither - and
    both are real: Workday's confirm box is `id="verifyPassword"` and an
    `api_key` field is `name="api_key"`. camelCase is split and the
    separators that identifiers use become spaces.
    """
    joined = " ".join(f or "" for f in fields)
    joined = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", joined)
    return re.sub(r"[_\-.\[\]]+", " ", joined)


def secret_field(type_: str = "", name: str = "", id_: str = "",
                 label: str = "", autocomplete: str = "") -> bool:
    """Whether this control's value may never be recorded.

    Read from the same four places a screen reader would, because a Workday
    password box is `data-automation-id="password"` with its label in a
    sibling and an iCIMS one is `name="pwd"` with no label at all.
    """
    if (type_ or "").strip().lower() in SECRET_TYPES:
        return True
    if SECRET_AUTOCOMPLETE.search(autocomplete or ""):
        return True
    return bool(SECRET_WORDS.search(words_in(name, id_, label)))


# --- 3. the person, out of the saved pages -------------------------------

# Shapes. A name has none, which is why `needles` exists beside this.
CARRIES = (
    re.compile(r"[^\s\"'<>@]+@[^\s\"'<>@]+\.[a-z]{2,}", re.I),       # an address
    re.compile(r"[^\s\"'<>]+\.(?:pdf|docx?|rtf|txt|odt|pages)\b", re.I),  # a document
    re.compile(r"\+?\d[\d().\-\s]{7,}\d"),                           # a number to ring
)


def scrub_shapes(text: str) -> str:
    for pattern in CARRIES:
        text = pattern.sub("[redacted]", text)
    return text


def needles(answers: dict) -> list[str]:
    """The person's own words, from the profile the fill already uses:
    longest first, so a full name goes before the halves of it."""
    found: set[str] = set()
    for key, value in (answers or {}).items():
        if not isinstance(value, str) or len(value.strip()) < 3:
            continue
        if any(word in key.lower() for word in
               ("name", "email", "phone", "address", "city", "linkedin",
                "github", "website", "portfolio")):
            found.add(value.strip())
    # Only a *name* is split into its parts. A city was split too, and
    # "Los Angeles" put "Los" on the list, which redacts the word wherever
    # it appears; a surname on its own is worth having, a place's first word
    # is not.
    for key, value in (answers or {}).items():
        if not isinstance(value, str) or "name" not in key.lower():
            continue
        whole = value.strip()
        if " " in whole and "@" not in whole and len(whole) < 60:
            found.update(part for part in whole.split() if len(part) > 3)
    return sorted(found, key=len, reverse=True)


def scrub(text: str, words: list[str], shapes: bool = False) -> tuple[str, list[str]]:
    """The text with every needle taken out, and which ones were found. A
    needle that is found is a hole somewhere upstream, so it is reported
    rather than quietly patched over.

    `shapes` is off by default and belongs to saved pages only. The phone
    pattern matches any long run of digits, which in a flow row is the
    millisecond timestamp and the loopback address - the first run of the
    tracker wrote `"at": [redacted]` on every line and called 127.0.0.1 a
    phone number. A keystroke log is meant to carry what was typed; it is
    the saved HTML that is a fixture and must carry nobody.
    """
    if shapes:
        text = scrub_shapes(text)
    hit = []
    for needle in words:
        if needle and needle.lower() in text.lower():
            hit.append("an address" if "@" in needle else needle)
            text = re.compile(re.escape(needle), re.I).sub("[redacted]", text)
    return text, hit
