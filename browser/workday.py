"""Workday, page by page, with no model on the form.

Every other system this app fills is one page: press Jobright's Autofill, put
the tailored documents on, answer what is open, stop. Workday is five or six
pages behind an account, and each one is the same three steps followed by
"Save and Continue". Doing that by hand after the tailoring is done is most of
the work the app exists to remove, so this module walks the pages.

What it does on each page:

1. presses Jobright's Autofill (`browser/autofill.py`, unchanged),
2. puts the tailored resume and cover letter on, where that page has slots,
   applies the corrections store, and answers the free-form questions with
   the tailor model (`browser/forms/engine.py`, unchanged),
3. presses the page's own "Save and Continue", which files nothing,
4. reads what Workday says back, and stops for the person when the page will
   not advance.

What it never does:

- **It never presses Submit.** `guard.describes_submit` is checked against
  every control before it is pressed, the review page is a terminal stage,
  and the review page's own Submit is the person's click, in their browser
  or through the review page's own button. This module has no path to it.
- It never presses **Apply** on a posting either. Workday's Apply is refused
  by the same guard, and the driver stops and asks instead: a posting page is
  not an application form, and pressing Apply is how an application starts on
  someone else's terms.
- It never answers a visa, sponsorship or work-authorisation question: those
  fields are skipped before anything is matched (`engine`), and a page whose
  only unfilled required fields are those stops for the person.
- It never reads anyone's email. A verification step is a pause, not a
  puzzle to solve.

Account creation is Jobright's: the driver presses Workday's "Create Account",
waits for Jobright's autofill to put the address and the password in, and
presses the account form's own button. No credential is read, written or kept
here; whatever Jobright fills is between the person and their browser.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import autofill, guard
from .autofill import Session

log = logging.getLogger(__name__)

HOSTS = ("myworkdayjobs.com", "myworkdaysite.com", "wd1.myworkdayjobs.com", ".workday.com")

MAX_PAGES = 10          # a Workday application is five or six; ten is a loop
SETTLE = 1.0
ADVANCE_TIMEOUT = 25.0  # how long a "Save and Continue" gets to change the page
STAGE_TIMEOUT = 40.0    # how long a page gets to render after a navigation


def is_workday(url: str) -> bool:
    return any(host in (url or "").lower() for host in HOSTS)


# The page's own words for moving on. None of these files an application:
# Workday's last page is a review and its button says Submit, which is
# refused here and everywhere else.
NEXT = re.compile(r"^\s*(save and continue|save & continue|continue|next|"
                  r"save and next|start your application|start application)\s*$", re.I)
# The link or tab that switches the sign-in box to the account box, and the
# account box's own button.
ACCOUNT_LINK = re.compile(r"^\s*(create account|create an account|sign up|new user)\s*$", re.I)
ACCOUNT_DO = re.compile(r"^\s*(create account|create my account|sign up|register)\s*$", re.I)
SIGN_IN = re.compile(r"^\s*(sign in|log in|login)\s*$", re.I)

VERIFY = re.compile(r"verify (your )?(e-?mail|account)|verification code|we (have |'ve )?sent|"
                    r"check your (e-?mail|inbox)|enter the code", re.I)
REVIEW = re.compile(r"\breview\b.{0,40}\b(submit|application)\b|^\s*review\s*$|"
                    r"review your application|please review", re.I)
APPLY_ONLY = re.compile(r"^\s*(apply|apply now|apply manually|autofill with resume|"
                        r"use my last application)\s*$", re.I)

# What Workday shows when a page will not advance.
ERROR_HINT = re.compile(r"error|required|must be|cannot be|please (enter|select|complete|provide)", re.I)


# Everything the driver needs to decide what this page is and what to press.
# One evaluate per look: Workday's pages are heavy and a round trip per
# question was slower than the fill itself.
PAGE_JS = r"""
(() => {
  const vis = (el) => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== "hidden" && s.display !== "none";
  };
  const buttons = [];
  for (const el of document.querySelectorAll("button, a[role=button], input[type=submit], [role=button], a")) {
    const text = (el.innerText || el.value || el.getAttribute("aria-label") || "").trim().slice(0, 80);
    if (!text || text.length > 80 || !vis(el)) continue;
    const r = el.getBoundingClientRect();
    buttons.push({
      text,
      disabled: !!(el.disabled || el.getAttribute("aria-disabled") === "true"),
      x: r.left + r.width / 2, y: r.top + r.height / 2, top: r.top,
    });
  }
  let editable = 0, files = 0, passwords = 0;
  for (const el of document.querySelectorAll("input, textarea, select")) {
    const t = (el.type || "").toLowerCase();
    if (t === "hidden" || t === "submit" || t === "button") continue;
    if (t === "file") { files++; continue; }
    if (t === "password") passwords++;
    editable++;
  }
  // What Workday says is wrong: its own error slots, then any field it has
  // marked invalid, named by the label a reader would use.
  const errors = [];
  for (const el of document.querySelectorAll(
      "[role=alert], [data-automation-id*='rror'], [class*='rror' i], [aria-invalid='true']")) {
    if (!vis(el)) continue;
    let text = (el.innerText || "").trim();
    if (el.getAttribute("aria-invalid") === "true") {
      const id = el.getAttribute("aria-labelledby");
      const label = (id && document.getElementById(id.split(/\s+/)[0]))
        || (el.labels && el.labels[0])
        || (el.closest("[data-automation-id]") && el.closest("[data-automation-id]").querySelector("label"));
      text = ((label && label.innerText) || el.getAttribute("aria-label") || "").trim() + " (needs a value)";
    }
    text = text.replace(/\s+/g, " ").trim();
    if (text && text.length < 200 && !errors.includes(text)) errors.push(text);
  }
  const heads = [...document.querySelectorAll("h1, h2, [role=heading]")]
    .filter(vis).map((el) => (el.innerText || "").trim().slice(0, 120)).filter(Boolean).slice(0, 8);
  return {
    url: location.href,
    title: document.title || "",
    heads,
    text: (document.body ? document.body.innerText : "").slice(0, 6000),
    buttons: buttons.slice(0, 120),
    editable, files, passwords,
    errors: errors.slice(0, 20),
  };
})()
"""

CLICK_AT_JS = r"""
(function (x, y) {
  const el = document.elementFromPoint(x, y);
  if (el) el.scrollIntoView({ block: "center", behavior: "instant" });
  return true;
})
"""

# The logo, on a review page where nothing is an input any more. Workday's
# review lists what was entered as text, so a mark can only be anchored to
# the words themselves: the row whose text is the value the automation set.
# Same drawing as the form's mark (`engine.LOGO_SVG`), same promise - this
# value is ours, not yours and not Jobright's.
MARK_TEXT_FN = r"""
(function (items, logo) {
  let done = 0;
  const seen = new Set();
  const place = (host, note, key) => {
    if (!host || seen.has(host)) return false;
    if (host.querySelector(":scope > [data-autopilot-mark]")) return false;
    const dot = document.createElement("span");
    dot.setAttribute("data-autopilot-mark", key);
    dot.title = "Filled by Autopilot: " + note;
    dot.setAttribute("aria-label", "Filled by Autopilot");
    dot.style.cssText = "display:inline-block;line-height:0;margin:0 8px 0 0;vertical-align:middle;flex:none;";
    dot.innerHTML = logo;
    host.insertBefore(dot, host.firstChild);
    seen.add(host);
    return true;
  };
  // The smallest element that holds the whole value: a bigger one would put
  // the logo in front of half the page.
  const smallest = (needle) => {
    let best = null;
    for (const el of document.querySelectorAll("div, span, li, td, p, dd, label, h3, h4")) {
      const text = (el.innerText || "").replace(/\s+/g, " ").trim();
      if (!text || text.length > 400 || !text.includes(needle)) continue;
      if (!best || text.length < (best.innerText || "").replace(/\s+/g, " ").trim().length) best = el;
    }
    return best;
  };
  for (const item of items) {
    const needle = (item.text || "").trim();
    if (needle.length < 3) continue;
    if (place(smallest(needle), item.note || "set by the assistant", needle.slice(0, 60))) done++;
  }
  return done;
})
"""


@dataclass
class Page:
    """One page of the application, as the driver saw and left it."""
    number: int
    stage: str
    url: str = ""
    heading: str = ""
    autofilled: str = ""
    resume_uploaded: bool = False
    cover_letter_uploaded: bool = False
    answered: list = field(default_factory=list)
    corrected: list = field(default_factory=list)
    advanced: bool = False
    errors: list = field(default_factory=list)


@dataclass
class WorkdayResult:
    """Where the walk got to, and why it stopped."""
    target_id: str = ""
    pages: list = field(default_factory=list)
    stage: str = ""              # the stage it stopped on
    reached_review: bool = False
    paused: str = ""             # a reason name when the person has to act
    detail: str = ""             # that reason in the person's words
    needs: list = field(default_factory=list)   # what is still empty, by label
    resume_uploaded: bool = False
    cover_letter_uploaded: bool = False
    answered: list = field(default_factory=list)
    errors: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """A walk that got the documents on and reached the review page with
        nothing left for the person is the whole job done to checkpoint 2."""
        return self.reached_review and not self.paused

    def summary(self) -> str:
        parts = [f"{len(self.pages)} page(s)"]
        parts.append("resume replaced" if self.resume_uploaded else "resume NOT replaced")
        if self.cover_letter_uploaded:
            parts.append("cover letter attached")
        if self.answered:
            parts.append(f"{len(self.answered)} question(s) answered")
        if self.reached_review:
            parts.append("reached the review page")
        if self.paused:
            parts.append(f"paused: {self.paused}")
        return "; ".join(parts)

    def to_json(self) -> dict:
        return {
            "target_id": self.target_id,
            "stage": self.stage,
            "reached_review": self.reached_review,
            "paused": self.paused,
            "detail": self.detail,
            "needs": self.needs,
            "resume_uploaded": self.resume_uploaded,
            "cover_letter_uploaded": self.cover_letter_uploaded,
            "answered": self.answered,
            "errors": self.errors,
            "pages": [vars(p) for p in self.pages],
        }


# What each pause means to the person reading it on the review page. The
# driver never guesses beyond these: every one of them is something only
# they can do.
PAUSES = {
    "needs_apply": "this is still the posting, not the form: press Apply in the browser "
                   "(Autopilot never presses Apply), then press Carry on",
    "needs_sign_in": "Workday wants an account: sign in or let Jobright create one in the "
                     "browser, then press Carry on",
    "verify_email": "Workday sent a verification email: open it and verify, then press Carry on",
    "blocked": "Workday will not move on until these are answered; fill them in the browser, "
               "then press Carry on",
    "stuck": "the page did not move on and said nothing about why; look at the browser, "
             "then press Carry on",
    "too_many_pages": "the application went round more pages than one ever should; "
                      "look at the browser",
}


def stage_of(look: dict) -> str:
    """What kind of Workday page this is: `account`, `verify`, `review`,
    `posting`, `form`, or `unknown`. Read off one look at the page."""
    text = str(look.get("text") or "")
    heads = " | ".join(look.get("heads") or [])
    buttons = [str(b.get("text") or "") for b in look.get("buttons") or []]

    if look.get("passwords"):
        return "account"
    if VERIFY.search(heads) or VERIFY.search(text[:1500]):
        return "verify"
    # A review page is the one with Submit on it and (almost) nothing to type.
    if any(guard.describes_submit(b) for b in buttons) and int(look.get("editable") or 0) <= 3:
        return "review"
    if REVIEW.search(heads):
        return "review"
    # A posting: an Apply button, no form under it.
    if int(look.get("editable") or 0) <= 2 and any(APPLY_ONLY.match(b) for b in buttons):
        return "posting"
    if int(look.get("editable") or 0) > 2 or int(look.get("files") or 0):
        return "form"
    return "unknown"


def next_button(look: dict) -> Optional[dict]:
    """The control that moves to the next page, or None.

    Refused before it is returned, not after: anything that reads as a
    submit is never a candidate however a page words it, and a disabled
    control is not one either. The lowest on the page wins, since Workday
    puts the page's own action at the foot and a header may repeat a word.
    """
    live = [b for b in look.get("buttons") or []
            if NEXT.match(str(b.get("text") or "")) and not b.get("disabled")
            and not guard.describes_submit(str(b.get("text") or ""))]
    return sorted(live, key=lambda b: b.get("top") or 0)[-1] if live else None


def account_button(look: dict, pattern: re.Pattern) -> Optional[dict]:
    live = [b for b in look.get("buttons") or []
            if pattern.match(str(b.get("text") or "")) and not b.get("disabled")
            and not guard.describes_submit(str(b.get("text") or ""))]
    return sorted(live, key=lambda b: b.get("top") or 0)[-1] if live else None


def blocking(look: dict) -> list[str]:
    """What Workday is complaining about, in its own words, de-duplicated
    and cut to something a person can read on a banner."""
    out = []
    for line in look.get("errors") or []:
        text = " ".join(str(line).split())
        if not text or text in out:
            continue
        if not ERROR_HINT.search(text) and "needs a value" not in text:
            continue
        out.append(text[:160])
    return out[:12]
