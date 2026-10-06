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
- It never presses **Submit**, and that is the only never here about pressing.
  It does press **Apply** on a posting (2026-09-30, `browser/open_apply.py`),
  under that module's own precondition: no file input, no password box and
  too few fields to be an application, so there is nothing on the page that
  could be sent. Refusing it did not protect anything - Workday's account
  page, which the person has to reach, does not exist until Apply is pressed.
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

from . import guard
from .autofill import Session

log = logging.getLogger(__name__)

HOSTS = ("myworkdayjobs.com", "myworkdaysite.com", "wd1.myworkdayjobs.com", ".workday.com")

MAX_PAGES = 10          # a Workday application is five or six; ten is a loop
SETTLE = 1.0
ADVANCE_TIMEOUT = 25.0  # how long a "Save and Continue" gets to change the page
STAGE_TIMEOUT = 40.0    # how long a page gets to render after a navigation
# A Workday step is not a navigation: `document.readyState` never leaves
# "complete", so nothing the loader waits on fires, and `advance` used to
# return the *first* look whose shape differed - which is the half-drawn
# page, before its fields exist. Three runs on disk show what that cost:
# "pressed by code, 0 - 13 fields" on pages two, three and four, meaning
# Jobright's autofill was pressed against a page with no fields on it, and
# with no job matched it offers "Autofill for Another Job", which opens a
# tab of its own. The page is settled when its shape has held still this
# many consecutive looks.
STABLE_LOOKS = 2
SETTLE_TIMEOUT = 15.0   # how long to wait for a page to stop changing


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
    # The documents the review page was able to vouch for by name.
    marked: list = field(default_factory=list)
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
    # A posting first, and only then a review page. `guard.describes_submit`
    # answers True to "Apply" - it is the deny-list for the agent, where
    # starting an application and sending one are equally forbidden - so a
    # Workday posting, which has an Apply button and nothing to type, read
    # as a review page and the walk stopped on it believing it was finished.
    if int(look.get("editable") or 0) <= 2 and any(APPLY_ONLY.match(b) for b in buttons):
        return "posting"
    # A review page is the one with Submit on it and (almost) nothing to type.
    if any(guard.describes_submit(b) for b in buttons) and int(look.get("editable") or 0) <= 3:
        return "review"
    if REVIEW.search(heads):
        return "review"
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


# --------------------------------------------------------------- the walk --
#
# Everything above is a question asked of one look at one page. This is the
# loop that asks them, page after page, and it is deliberately dull: look,
# decide, do one thing, look again. Nothing here is a guess about what
# Workday meant - a page it cannot name is a page it stops on.

CHANGE_KEYS = ("url", "heads", "editable", "files")


async def look_at(page: Session) -> dict:
    return (await page.evaluate(PAGE_JS)) or {}


def _shape(look: dict) -> tuple:
    """What "the page changed" means: a new URL, new headings, or a
    different number of boxes. Workday's steps share a URL more often than
    not, so the URL alone cannot answer it."""
    return (str(look.get("url") or ""), " | ".join(look.get("heads") or []),
            int(look.get("editable") or 0), int(look.get("files") or 0))


async def click(page: Session, button: dict) -> None:
    """Press a control at its centre with a real mouse event. The caller has
    already refused it if it reads as a submit; this only presses."""
    await page.evaluate(f"({CLICK_AT_JS})({button['x']!r}, {button['y']!r})")
    await asyncio.sleep(SETTLE / 2)
    for kind in ("mousePressed", "mouseReleased"):
        await page.send("Input.dispatchMouseEvent", {
            "type": kind, "x": button["x"], "y": button["y"],
            "button": "left", "clickCount": 1,
        })


async def settled(page: Session, timeout: Optional[float] = None) -> dict:
    """Look until the page stops changing, then return that look.

    Nothing is pressed on a page that is still drawing itself. A Workday
    step replaces the page's contents without a navigation, so there is no
    load event to wait on and the first look after a step is of a page with
    no fields yet; pressing Jobright's autofill into that gap is what the
    stray blank tabs were (it has no job matched, offers "Autofill for
    Another Job", and that control opens a tab).

    The timeout is read here rather than frozen into the signature, which is
    the trap this file has been bitten by before.
    """
    deadline = time.monotonic() + (SETTLE_TIMEOUT if timeout is None else timeout)
    look: dict = {}
    shape: Optional[tuple] = None
    same = 0
    while time.monotonic() < deadline:
        await asyncio.sleep(SETTLE)
        try:
            look = await look_at(page)
        except Exception:  # noqa: BLE001 - mid-render, look again
            same = 0
            continue
        now = _shape(look)
        same = same + 1 if now == shape else 0
        shape = now
        if same >= STABLE_LOOKS:
            break
    return look


async def advance(page: Session, before: tuple, timeout: Optional[float] = None) -> dict:
    """Wait for the page to become a different page, and then for it to
    finish becoming one. Returns the look it settled on; the caller compares
    the shapes to see whether it moved.

    The timeout is read here, not frozen into the signature: a module-level
    `Path`/float default is bound at import, so a test that lowers it changes
    nothing and waits the real twenty-five seconds a page.
    """
    deadline = time.monotonic() + (ADVANCE_TIMEOUT if timeout is None else timeout)
    look: dict = {}
    while time.monotonic() < deadline:
        await asyncio.sleep(SETTLE)
        try:
            look = await look_at(page)
        except Exception:  # noqa: BLE001 - mid-navigation, look again
            continue
        if look and _shape(look) != before:
            # It has started changing; wait for it to stop before anybody
            # reads it or presses anything on it.
            return await settled(page) or look
    return look


async def walk(cdp_url: str, target_id: str, resume=None, cover_letter=None,
               answerer=None, corrections=None, max_pages: int = MAX_PAGES) -> WorkdayResult:
    """Walk one Workday application, page by page, and stop at the review.

    On each page: Jobright's autofill, then the tailored documents and the
    open questions by code, then the page's own "Save and Continue". Never
    Submit - the review page is where this ends, which is checkpoint 2, and
    the person's own click is what follows it.

    Stops and says why whenever the next step is the person's: an account,
    an email to verify, a question Workday will not let pass, or a page that
    would not move.
    """
    from . import autofill, open_apply, signin
    from .forms import Adapter
    from .forms.engine import mark_named_file, run_documents

    result = WorkdayResult(target_id=target_id)
    applies = 0
    async with autofill.attached(cdp_url, target_id=target_id) as (page, _):
        await page.send("DOM.enable")
        # Nothing is read or pressed until the first page has stopped
        # drawing itself; the tab has just been opened or has just come back
        # from Apply, and a Workday page arrives in pieces.
        await settled(page)
        for number in range(1, max_pages + 1):
            try:
                look = await look_at(page)
            except Exception as error:  # noqa: BLE001
                result.errors.append(f"reading page {number}: {error}")
                break
            stage = stage_of(look)
            result.stage = stage
            step = Page(number=number, stage=stage, url=str(look.get("url") or ""),
                        heading=(look.get("heads") or [""])[0])
            result.pages.append(step)

            if stage == "posting":
                # The same press the sign-in watch makes, with the same
                # precondition: a posting has nothing on it that could be
                # sent. Anything else about this page is the person's.
                if applies >= open_apply.MAX_PRESSES:
                    result.paused, result.detail = "needs_apply", PAUSES["needs_apply"]
                    break
                shot = signin.Look(url=str(look.get("url") or ""),
                                   fields=int(look.get("editable") or 0),
                                   files=int(look.get("files") or 0),
                                   passwords=int(look.get("passwords") or 0))
                label = await open_apply.press(page, shot)
                if not label:
                    result.paused, result.detail = "needs_apply", PAUSES["needs_apply"]
                    break
                applies += 1
                step.advanced = bool(await advance(page, _shape(look)))
                continue

            if stage == "account":
                result.paused, result.detail = "needs_sign_in", PAUSES["needs_sign_in"]
                break
            if stage == "verify":
                result.paused, result.detail = "verify_email", PAUSES["verify_email"]
                break
            if stage == "review":
                # The end of the line. Submit is the person's, from the page
                # or from the review page's own button.
                result.reached_review = True
                # The documents went on three pages ago, and every page of a
                # Workday application replaces the last - so the logo, the
                # upload block it hung off and the ref it was placed against
                # are all gone by here, and the review shows a filename with
                # nothing to say whose it is. "I cannot tell if the resume
                # has been changed" is the whole point of the mark, and the
                # review is the page where it is asked. Named files only:
                # nothing else on the page is touched.
                for path, note in ((resume, "the tailored resume"),
                                   (cover_letter, "the cover letter")):
                    if path is None:
                        continue
                    try:
                        if await mark_named_file(page, Path(path).name, note):
                            result.marked.append(Path(path).name)
                    except Exception as error:  # noqa: BLE001 - the logo is advice
                        result.errors.append(f"marking {Path(path).name}: {error}")
                break
            if stage != "form":
                result.paused, result.detail = "stuck", PAUSES["stuck"]
                break

            # Jobright first, in this tab, so its autofill has this page's
            # fields before ours goes over them.
            try:
                pressed = await autofill.in_tab(cdp_url, target_id)
                step.autofilled = pressed.summary() if pressed else ""
            except Exception as error:  # noqa: BLE001 - ours still runs
                step.errors.append(f"jobright autofill: {error}")

            # The documents belong to the page that has a slot for them, and
            # only until they are on: Workday asks for a resume once.
            want_resume = resume if (look.get("files") and not result.resume_uploaded) else None
            want_cover = cover_letter if (look.get("files") and not result.cover_letter_uploaded) else None
            try:
                report = await run_documents(page, Adapter(), target_id, want_resume, want_cover,
                                             answerer, corrections)
                step.resume_uploaded = report.resume_uploaded
                step.cover_letter_uploaded = report.cover_letter_uploaded
                step.answered = list(report.answered)
                step.corrected = list(report.corrected)
                result.resume_uploaded = result.resume_uploaded or report.resume_uploaded
                result.cover_letter_uploaded = result.cover_letter_uploaded or report.cover_letter_uploaded
                result.answered.extend(report.answered)
                # A page with no file input is not a broken page here: it is
                # page one of five, and the slot is on page two.
                step.errors.extend(e for e in report.errors if want_resume or "resume" not in e.lower())
            except Exception as error:  # noqa: BLE001
                step.errors.append(f"documents: {error}")
            result.errors.extend(step.errors)

            before = _shape(await look_at(page))
            button = next_button(look if _shape(look) == before else await look_at(page))
            if button is None:
                stop = blocking(look)
                result.needs = stop
                result.paused = "blocked" if stop else "stuck"
                result.detail = PAUSES[result.paused]
                break
            await click(page, button)
            after = await advance(page, before)
            step.advanced = bool(after) and _shape(after) != before
            if not step.advanced:
                stop = blocking(after or look)
                result.needs = stop
                result.paused = "blocked" if stop else "stuck"
                result.detail = PAUSES[result.paused]
                break
        else:
            result.paused, result.detail = "too_many_pages", PAUSES["too_many_pages"]
    return result
