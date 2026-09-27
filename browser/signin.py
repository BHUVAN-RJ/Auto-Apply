"""Two detectors, kept apart: is this a sign-in, and is this the form?

A lot of employers keep the application behind an account — Workday, Oracle,
McKinsey, CVS — and the app never presses Apply and never types a credential.
Until now that ended the run: the fill found no form, wrote `needs_sign_in.txt`
and handed the job back, so the person signed in, came back to the review page
and pressed Fill again. The tailored documents were fine; the whole round trip
existed because nobody was watching the tab.

So the flow comes apart into two questions that are asked independently:

1. **Is the tab on a sign-in right now?** (`looks_like_signin`) — a password
   box, a sign-in heading, "Continue with Google", or the tab sitting on an
   identity provider's own host.
2. **Is the tab on an application form?** (`looks_like_form`) — somewhere to
   attach a resume, or enough real fields, on the job's own site.

Neither knows about the other, which is the point: single sign-on leaves the
employer's site entirely (accounts.google.com, login.microsoftonline.com, an
Okta tenant) and comes back, so a detector that answers "form?" by looking at
the URL is wrong for the whole middle of the flow. They are joined only by
`wait_for_form`, which follows **the tab**, not the link: a CDP target id is
stable across navigation, so the same tab is watched through the provider and
back to the employer, and the moment the second detector fires the fill starts
from the first page as if the wall had never been there.

Nothing here types, clicks, or reads a credential. Signing in is the person's,
in their own browser; this only notices when they are through.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit

from .autofill import Session, attached

# How long a person gets to sign in before the fill gives up and hands the job
# back the old way. Generous on purpose: a Workday account with an email
# verification in the middle is minutes, and the cost of waiting is a tab
# sitting open, while the cost of giving up early is the round trip this
# module exists to remove.
SIGNIN_TIMEOUT = 600.0
POLL = 2.0

# The hosts a sign-in leaves for. The tab is still the job's tab while it is
# here; only the link has gone somewhere else.
IDENTITY_HOSTS = (
    "accounts.google.com", "login.microsoftonline.com", "login.live.com",
    "appleid.apple.com", "www.linkedin.com/oauth", "linkedin.com/oauth",
    "github.com/login", "okta.com", "oktapreview.com", "auth0.com",
    "onelogin.com", "pingidentity.com", "signin.aws.amazon.com",
    "id.workday.com", "authn.workday.com", "wd1.myworkday.com/wday/authgwy",
)

SIGNIN_WORDS = re.compile(
    r"\bsign in\b|\bsign-in\b|\blog in\b|\blogin\b|\bcreate account\b|"
    r"\bforgot (your )?password\b|\bcontinue with (google|apple|microsoft|linkedin|github)\b|"
    r"\bsign in with\b|\bverify (your )?e-?mail\b|\bverification code\b|"
    r"\bpassword\b.{0,40}\bremember me\b", re.I)

# "Log in" is in half the job descriptions in the country - "build the portal
# where users log in to manage their orders" - so the words alone decide
# nothing. Two ways a page is really offering an account:
#
# ALWAYS is a phrase nobody writes in prose; it is a button or nothing.
ALWAYS = re.compile(r"\bcontinue with (google|apple|microsoft|linkedin|github)\b|"
                    r"\bsign in with\b|\bforgot (your )?password\b|"
                    r"\bverification code\b|\bverify (your )?e-?mail\b", re.I)
# ON_ITS_OWN is a phrase that is a control when it *is* the whole line, and
# prose when it is buried in one. A heading and a button are short lines; a
# requirement in a description is not.
ON_ITS_OWN = re.compile(r"^(sign in|sign-in|log in|login|create (an )?account|"
                        r"sign up|new user|use my last application)"
                        r"( to (continue|your account|apply))?[.:!]?$", re.I)
SHORT_LINE = 40


def identity_host(url: str) -> bool:
    """Whether this URL belongs to somebody's single sign-on, not the
    employer. The tab is still the job's tab; the link is on a detour."""
    host = (urlsplit(url or "").hostname or "").lower()
    whole = f"{host}{urlsplit(url or '').path}".lower()
    return any(host == h or host.endswith("." + h) or h in whole for h in IDENTITY_HOSTS)


# One look at the tab, enough for both questions to be asked of it. Kept
# separate from `workday.PAGE_JS`: that one is about which page of an
# application this is, and this one is about whether an application is
# reachable at all.
LOOK_JS = r"""
(() => {
  const vis = (el) => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== "hidden" && s.display !== "none";
  };
  let fields = 0, files = 0, passwords = 0;
  for (const el of document.querySelectorAll("input, textarea, select")) {
    const t = (el.type || "").toLowerCase();
    if (t === "hidden" || t === "submit" || t === "button") continue;
    if (!vis(el)) continue;
    if (t === "file") { files++; continue; }
    if (t === "password") { passwords++; continue; }
    fields++;
  }
  return {
    url: location.href,
    title: document.title || "",
    text: (document.body ? document.body.innerText : "").slice(0, 4000),
    fields, files, passwords,
  };
})()
"""


@dataclass
class Look:
    url: str = ""
    title: str = ""
    text: str = ""
    fields: int = 0
    files: int = 0
    passwords: int = 0

    @classmethod
    def of(cls, raw: Optional[dict]) -> "Look":
        raw = raw or {}
        return cls(url=str(raw.get("url") or ""), title=str(raw.get("title") or ""),
                   text=str(raw.get("text") or ""), fields=int(raw.get("fields") or 0),
                   files=int(raw.get("files") or 0), passwords=int(raw.get("passwords") or 0))


# ------------------------------------------------------------ detector one --

def looks_like_signin(look: Look) -> bool:
    """Is the tab on a sign-in, an account creation, or a verification step?

    A password box is decisive. Otherwise the page has to be *offering* an
    account — "Sign in", "Create account", "Continue with Google" — and have
    almost nothing else on it: a posting that says "log in to our portal"
    somewhere in its description is not a sign-in.
    """
    if look.passwords:
        return True
    if identity_host(look.url):
        return True
    if look.files:
        # Somewhere to attach a resume is the application, whatever the
        # header above it says.
        return False
    head = f"{look.title}\n{look.text[:1500]}"
    if ALWAYS.search(head):
        return True
    offered = any(ON_ITS_OWN.match(line.strip())
                  for line in head.splitlines() if len(line.strip()) <= SHORT_LINE)
    return offered and look.fields <= 4


# ------------------------------------------------------------ detector two --

# The same floor the fill uses: everything this app does on a form is built
# around putting the tailored resume on it, so a page with nowhere to attach
# one is not the application yet - unless it is plainly a form of its own
# with real questions on it, which is how Workday's first page reads.
FORM_FIELDS = 5


def looks_like_form(look: Look, job_url: str = "") -> bool:
    """Is the tab on an application form?

    Asked with no reference to the sign-in: a page either has somewhere to
    put the resume and questions to answer, or it does not. `job_url` only
    keeps the answer honest about *whose* form it is, so a provider's own
    page full of boxes is never mistaken for the application.
    """
    if looks_like_signin(look):
        return False
    if identity_host(look.url):
        return False
    # `job_url` is deliberately not a host check beyond the provider veto
    # above: an employer's application often lives on a different host from
    # the posting (a careers page linking to its Workday tenant), and a form
    # rejected for that reason is the round trip this module removes.
    return look.files > 0 or look.fields >= FORM_FIELDS


def same_site(a: str, b: str) -> bool:
    ha = (urlsplit(a or "").hostname or "").lower().removeprefix("www.")
    hb = (urlsplit(b or "").hostname or "").lower().removeprefix("www.")
    if not ha or not hb:
        return False
    return ha == hb or ha.endswith("." + hb) or hb.endswith("." + ha)


# ------------------------------------------------------------- the joining --

@dataclass
class Wait:
    """What happened while we watched the tab."""
    target_id: str = ""
    signed_in: bool = False     # a wall was there and it cleared
    saw_signin: bool = False    # a wall was there at all
    ready: bool = False         # the form is on the screen now
    url: str = ""
    waited: float = 0.0
    note: str = ""


async def look_at(page: Session) -> Look:
    return Look.of(await page.evaluate(LOOK_JS))


async def wait_for_form(cdp_url: str, target_id: str, job_url: str = "",
                        timeout: float = SIGNIN_TIMEOUT, notify=None) -> Wait:
    """Watch one tab until its application form is on the screen.

    Follows the tab, never the link: single sign-on takes the page to
    Google, to Microsoft, to an Okta tenant and back, and the CDP target id
    is the one thing that holds still through all of it. Returns as soon as
    the second detector fires, so the caller can fill from the first page as
    though the wall had never been there.

    Nothing is typed and nothing is pressed. `notify` is called with a short
    line whenever the answer changes, for the banner on the tab.
    """
    result = Wait(target_id=target_id)
    started = time.monotonic()
    said = ""
    async with attached(cdp_url, target_id=target_id) as (page, _):
        while True:
            try:
                look = await look_at(page)
            except Exception:  # noqa: BLE001 - mid-navigation; look again
                look = Look()
            result.url = look.url or result.url
            if looks_like_form(look, job_url):
                result.ready = True
                result.signed_in = result.saw_signin
                break
            if looks_like_signin(look):
                result.saw_signin = True
                line = ("Waiting while you sign in — I will carry on from the first page"
                        if not identity_host(look.url)
                        else "Waiting while you finish signing in with your provider")
                if notify and line != said:
                    said = line
                    await _say(notify, line)
            result.waited = time.monotonic() - started
            if result.waited >= timeout:
                result.note = ("no application form appeared while waiting to be signed in"
                               if result.saw_signin else "no application form on this page")
                break
            await asyncio.sleep(POLL)
    result.waited = time.monotonic() - started
    return result


async def ready_tab(cdp_url: str, url: str, timeout: float = SIGNIN_TIMEOUT,
                    notify=None) -> tuple[str, Wait]:
    """The tab this job's form is on, once it is reachable.

    Reuses the tab already open on the job (the one the injector screened,
    or a sign-in the person started themselves) rather than opening a
    second beside it, and otherwise opens one. Then waits, watching that
    tab, for the form. Returns (target id, what happened).
    """
    from . import autofill

    target_id = autofill.find_tab(cdp_url, url)
    if not target_id:
        async with attached(cdp_url, url) as (_, opened):
            target_id = opened
    wait = await wait_for_form(cdp_url, target_id, url, timeout=timeout, notify=notify)
    wait.target_id = target_id
    return target_id, wait


async def _say(notify, line: str) -> None:
    try:
        outcome = notify(line)
        if asyncio.iscoroutine(outcome):
            await outcome
    except Exception:  # noqa: BLE001 - the banner is advice
        pass
