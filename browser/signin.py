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

Nothing here types or reads a credential. Signing in is the person's, in their
own browser; this only notices when they are through. The one thing it does
press is a posting's **Apply**, and only when the page has nothing on it that
could be sent (`open_apply.safe_to_press`): the sign-in the person is waiting
to be shown cannot appear until Apply is pressed, so refusing to press it did
not protect anything - it just left the tab on the posting until the timeout.
Checkpoint 2, the filled form, is untouched; nothing here can reach a Submit
control.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit

from . import open_apply
from .autofill import Session, attached

# The same pattern the presser aims at, so the two never disagree about what
# an Apply button is.
_APPLY_SOURCE = json.dumps(open_apply.APPLY_START.pattern)

# How long a person gets to sign in before the fill gives up and hands the job
# back the old way. Generous on purpose: a Workday account with an email
# verification in the middle is minutes, and the cost of waiting is a tab
# sitting open, while the cost of giving up early is the round trip this
# module exists to remove.
SIGNIN_TIMEOUT = 600.0
# A posting that never becomes a form is not somebody halfway through an
# account: it is a page where nothing is happening, and the fill queue runs
# one job at a time. So the generous timeout is only for a wall that is
# actually there. Without one, the watch gives up after this instead - long
# enough for Apply to be pressed and the account page to render, short enough
# that an unattended Workday job costs a minute rather than ten (2026-09-30).
NO_SIGNIN_TIMEOUT = 90.0
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
  const APPLY = new RegExp(APPLY_PATTERN, "i");
  let fields = 0, files = 0, passwords = 0, apply = 0;
  for (const el of document.querySelectorAll("button, a, input[type=submit], [role=button]")) {
    const text = (el.innerText || el.value || el.getAttribute("aria-label") || "").trim();
    if (!text || text.length > 60 || !APPLY.test(text)) continue;
    const r = el.getBoundingClientRect();
    if (r.width > 0 && r.height > 0) apply++;
  }
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
    fields, files, passwords, apply,
  };
})()
""".replace("APPLY_PATTERN", _APPLY_SOURCE)


@dataclass
class Look:
    url: str = ""
    title: str = ""
    text: str = ""
    fields: int = 0
    files: int = 0
    passwords: int = 0
    apply: int = 0              # controls offering to start an application

    @classmethod
    def of(cls, raw: Optional[dict]) -> "Look":
        raw = raw or {}
        return cls(url=str(raw.get("url") or ""), title=str(raw.get("title") or ""),
                   text=str(raw.get("text") or ""), fields=int(raw.get("fields") or 0),
                   files=int(raw.get("files") or 0), passwords=int(raw.get("passwords") or 0),
                   apply=int(raw.get("apply") or 0))


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
    if look.apply:
        # A page offering to start an application is a posting, even when its
        # header carries a "Sign In" link - which every Workday tenant's does
        # (Globus Medical, 2026-09-30: "Sign In" on its own line, no fields,
        # so the words alone made a posting read as a wall, and the Apply
        # sitting on the same page was never pressed). A password box is
        # decisive and was tested above; this is only about the words.
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
    pressed: int = 0            # how many Apply controls were pressed
    applied: str = ""           # the label of the last one


async def look_at(page: Session) -> Look:
    return Look.of(await page.evaluate(LOOK_JS))


async def wait_for_form(cdp_url: str, target_id: str, job_url: str = "",
                        timeout: float = SIGNIN_TIMEOUT, notify=None,
                        press_apply: bool = True, patient: bool = False) -> Wait:
    """Watch one tab until its application form is on the screen.

    Follows the tab, never the link: single sign-on takes the page to
    Google, to Microsoft, to an Okta tenant and back, and the CDP target id
    is the one thing that holds still through all of it. Returns as soon as
    the second detector fires, so the caller can fill from the first page as
    though the wall had never been there.

    Nothing is typed and no credential is touched. A posting's Apply is
    pressed (`press_apply`, and only on a page with nothing to send), because
    the account is not offered until it is. `notify` is called with a short
    line whenever the answer changes, for the banner on the tab.

    `patient` gives the page the whole timeout even though no sign-in was
    ever recognised. It is for the systems whose wall we know is there and
    cannot reliably see - Workday, whose posting header carries "Sign In" on
    every tenant and whose account page is reached by a press this no longer
    makes. There the short no-wall cutoff was answering "no application form
    on this page" while the person was still typing their password.
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
            elif patient:
                # Told to wait, and nothing on the page says why yet: say so
                # rather than leaving the badge silent for ten minutes.
                line = "Waiting for you to open the application and sign in — I carry on from page 1"
                if notify and line != said:
                    said = line
                    await _say(notify, line)
            elif press_apply and result.pressed < open_apply.MAX_PRESSES:
                # A posting, with nothing on it that could be sent. Pressing
                # its Apply is what the person approved at checkpoint 1; the
                # sign-in they are waiting to be asked for cannot appear
                # until it is pressed. Checkpoint 2 is untouched: there is no
                # path from here to a Submit control.
                try:
                    label = await open_apply.press(page, look)
                except Exception:  # noqa: BLE001 - then wait as before
                    label = None
                if label:
                    result.pressed += 1
                    result.applied = label
                    if notify:
                        await _say(notify, f"Opening the application ({label})")
                    await asyncio.sleep(open_apply.SETTLE)
            result.waited = time.monotonic() - started
            # The full wait belongs to a wall that is really there. A posting
            # nobody is standing in front of gives up quickly instead, so one
            # job does not hold the one-at-a-time fill queue for ten minutes.
            limit = timeout if (patient or result.saw_signin) else min(timeout, NO_SIGNIN_TIMEOUT)
            if result.waited >= limit:
                result.note = ("no application form appeared while waiting to be signed in"
                               if result.saw_signin or patient
                               else "no application form on this page")
                break
            await asyncio.sleep(POLL)
    result.waited = time.monotonic() - started
    return result


async def ready_tab(cdp_url: str, url: str, timeout: float = SIGNIN_TIMEOUT,
                    notify=None, press_apply: bool = True,
                    patient: bool = False) -> tuple[str, Wait]:
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
    wait = await wait_for_form(cdp_url, target_id, url, timeout=timeout, notify=notify,
                               press_apply=press_apply, patient=patient)
    wait.target_id = target_id
    return target_id, wait


async def _say(notify, line: str) -> None:
    try:
        outcome = notify(line)
        if asyncio.iscoroutine(outcome):
            await outcome
    except Exception:  # noqa: BLE001 - the banner is advice
        pass
