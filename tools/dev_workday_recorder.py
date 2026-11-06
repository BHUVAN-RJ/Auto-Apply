"""DEVELOPER TOOL — records how a person walks a Workday application.

**This file is meant to be deleted.** It is not imported by the server, the
pipeline, the fill or anything under `browser/`; nothing starts it but a
person in a terminal, and `rm tools/dev_workday_recorder.py
tools/dev_workday_replay.py` removes the whole feature. It refuses to run
after `EXPIRES` for exactly that reason: a recorder nobody remembers is a
recorder nobody consented to.

Why it exists (2026-10-09): every Workday bug so far has been found by
reading one live page after the fact - the bin icon with no label, the
heading nine ancestors up, the account step that cannot be automated. A
screenshot is not enough to rebuild a flow from, and the suite's fixtures
were hand-made and shallower than the real thing, which is how a green test
certified a broken clear. So: watch a real person do five real Workday
applications, keep the pages and the moves, and rebuild the flow locally
(`dev_workday_replay.py`) to develop against.

**What it records: shape, never content.** The person's own details are not
interesting and are not kept.

- Every element it touches is identified by `data-automation-id`, `id`,
  `aria-label`, label text and a CSS path - what a selector needs.
- A value typed into a field is recorded as `<text:12chars>`: the kind and
  the length, never the characters. Passwords are not recorded at all, not
  even their length.
- A choice made from the page's own list (a `<select>` option, a radio, a
  Workday multi-select pill) keeps the *option's* label, because that text
  belongs to the employer's form rather than to the person: "Bachelor's
  Degree" is how the form is built, and knowing which control to drive is
  the point of this exercise.
- A file put on a slot is recorded as `<pdf:39chars>` with a count and a
  `repeated` flag - enough to see two copies of one resume on one slot,
  which is the bug this was built after, and not the filename, which is
  named after the person.
- Saved HTML has every `value=`, every `<textarea>` body and every selected
  pill scrubbed before it is written to disk.
- Nothing is recorded on an identity provider (Google, Microsoft, Okta,
  `id.workday.com`): signing in is the person's business, and the only fact
  worth keeping about it is that it happened, which the flow shows anyway.

It presses nothing, types nothing and closes nothing. It is a reader.

Run it:

    uv run python -m tools.dev_workday_recorder

Then apply for Workday jobs as usual. One directory per tab per visit under
`data/dev_recordings/workday/`, which is gitignored (the repository is
public, and these files describe a real application).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import paths  # noqa: E402
from browser import chrome, signin, workday  # noqa: E402
from browser.autofill import attached  # noqa: E402

# The one day this was asked for. Past it the tool refuses to run rather
# than quietly keeping on: "let's record only today" (2026-10-09).
EXPIRES = date(2026, 11, 2)

OUT = paths.DATA / "dev_recordings" / "workday"
POLL = 1.0          # how often the event buffer is drained
SHAPE_POLL = 2.0    # how often the page is checked for having become another

# ---------------------------------------------------------------- the page --

# Injected into every Workday tab, on this document and on each new one.
# Buffers what the person does on `window.__devRec`, which the poller drains.
# It listens; it never acts. Capture phase, so a control that stops the event
# from bubbling (Workday's do) is still seen.
RECORDER_JS = r"""
(function () {
  if (window.__devRecInstalled) return "already";
  window.__devRecInstalled = true;
  window.__devRec = [];

  const MAX = 500;                       // a flow is a few hundred moves
  const SECRET = /pass|secret|otp|code|token|ssn|social/i;

  const text = (el) => ((el && (el.innerText || el.textContent)) || "")
      .replace(/\s+/g, " ").trim();

  // What a reader would call this control, and what a selector would use.
  const label = (el) => {
    if (!el) return "";
    const by = el.getAttribute && el.getAttribute("aria-labelledby");
    if (by) {
      const names = by.split(/\s+/).map((id) => document.getElementById(id)).filter(Boolean);
      if (names.length) return names.map(text).join(" ").slice(0, 120);
    }
    if (el.getAttribute && el.getAttribute("aria-label")) return el.getAttribute("aria-label").slice(0, 120);
    if (el.labels && el.labels.length) return [...el.labels].map(text).join(" ").slice(0, 120);
    const own = el.closest && el.closest("label");
    if (own) return text(own).slice(0, 120);
    const field = el.closest && el.closest("[data-automation-id^='formField'], .css-7t35fz");
    if (field) {
      const lab = field.querySelector("label, legend");
      if (lab) return text(lab).slice(0, 120);
    }
    return "";
  };

  // A path that stands a chance of finding this element again: the coded
  // names Workday puts on things first, a structural path as the fallback.
  const path = (el) => {
    const steps = [];
    let node = el;
    for (let i = 0; node && node.nodeType === 1 && i < 12; i++, node = node.parentElement) {
      const auto = node.getAttribute("data-automation-id");
      if (auto) { steps.unshift(`[data-automation-id="${auto}"]`); break; }
      if (node.id && !/^\d/.test(node.id)) { steps.unshift(`#${node.id}`); break; }
      const tag = node.tagName.toLowerCase();
      const parent = node.parentElement;
      if (!parent) { steps.unshift(tag); break; }
      const same = [...parent.children].filter((c) => c.tagName === node.tagName);
      steps.unshift(same.length > 1 ? `${tag}:nth-of-type(${same.indexOf(node) + 1})` : tag);
    }
    return steps.join(" > ").slice(0, 300);
  };

  // The one rule: the shape of what was typed, never the characters. A
  // password is not even measured.
  const shapeOf = (el) => {
    const kind = (el.type || el.tagName || "").toLowerCase();
    const named = `${el.name || ""} ${el.id || ""} ${label(el)}`;
    if (kind === "password" || SECRET.test(named)) return "<secret>";
    const n = (el.value || "").length;
    if (!n) return "<empty>";
    return `<${kind === "textarea" ? "text" : kind || "text"}:${n}chars>`;
  };

  const describe = (el) => el && el.nodeType === 1 ? {
    tag: el.tagName.toLowerCase(),
    type: el.getAttribute("type") || "",
    auto: el.getAttribute("data-automation-id") || "",
    id: el.id || "",
    name: el.getAttribute("name") || "",
    role: el.getAttribute("role") || "",
    aria: el.getAttribute("aria-label") || "",
    label: label(el),
    // The control's own words: a button says "Save and Continue", an option
    // says "Bachelor's Degree". This is the form's text, not the person's.
    says: text(el).slice(0, 120),
    path: path(el),
  } : null;

  const push = (kind, el, extra) => {
    if (window.__devRec.length >= MAX) return;
    window.__devRec.push(Object.assign({
      kind: kind, at: Date.now(), url: location.href.split("?")[0],
      el: describe(el),
    }, extra || {}));
  };

  // What the person does. Capture phase: Workday's controls swallow events.
  document.addEventListener("click", (e) => {
    const el = e.target.closest("button, a, [role=button], [role=option], input, label, li, [data-automation-id]")
      || e.target;
    push("click", el, { trusted: e.isTrusted });
  }, true);

  document.addEventListener("change", (e) => {
    const el = e.target;
    if (!el || !el.tagName) return;
    if (el.tagName === "SELECT") {
      const opt = el.options[el.selectedIndex];
      push("choose", el, { chose: opt ? text(opt).slice(0, 120) : "", index: el.selectedIndex });
    } else if (el.type === "checkbox" || el.type === "radio") {
      push("toggle", el, { on: !!el.checked });
    } else if (el.type === "file") {
      // How many files are on a slot, and whether two of them are the same
      // file, is the whole question the two-resume bug turned on - and the
      // name itself is the person's (their resume is called after them). So:
      // the extension, the length, and whether the names repeat.
      const names = [...(el.files || [])].map((f) => f.name);
      push("file", el, {
        files: names.map((n) => `<${(n.split(".").pop() || "file").toLowerCase()}:${n.length}chars>`),
        count: names.length,
        repeated: new Set(names).size !== names.length,
      });
    } else {
      push("type", el, { shape: shapeOf(el) });
    }
  }, true);

  // Typing is noisy; one event per field once it is left alone.
  let timer = null, pending = null;
  document.addEventListener("input", (e) => {
    const el = e.target;
    if (!el || !el.tagName || el.type === "file") return;
    // A box and a radio are reported by `change` as a toggle, which says
    // more; without this they were reported twice, the second time as a
    // nonsense "<checkbox:2chars>".
    if (el.type === "checkbox" || el.type === "radio") return;
    pending = el;
    clearTimeout(timer);
    timer = setTimeout(() => {
      if (pending) push("type", pending, { shape: shapeOf(pending) });
      pending = null;
    }, 900);
  }, true);

  window.addEventListener("beforeunload", () => push("leave", document.body, {}));
  return "installed";
})()
"""

# Drains the buffer. One call, one round trip.
DRAIN_JS = "(() => { const out = window.__devRec || []; window.__devRec = []; return JSON.stringify(out); })()"

# The page as it stands: enough to tell one step from the next, plus the
# HTML with every value taken out of it.
SNAPSHOT_JS = r"""
(() => {
  const text = (el) => ((el && (el.innerText || el.textContent)) || "").replace(/\s+/g, " ").trim();
  const heads = [...document.querySelectorAll("h1,h2,h3,h4")].map(text).filter(Boolean).slice(0, 8);
  const fields = [...document.querySelectorAll("input,select,textarea")]
      .filter((el) => el.type !== "hidden");
  // Shaped the way `workday.stage_of` reads a look, so the recording can be
  // handed straight to the real code rather than to a copy of it.
  // The account button's text is the person's initials, which is the person.
  // The metadata is read off the live page, before the clone the scrub
  // happens on, so it is done here as well or not at all.
  const MINE = "#accountSettingsButton,[data-automation-id='ariaLiveMessage'],"
      + "[data-automation-id='selectedItem'],[data-automation-id='file-upload-item-name']";
  const buttons = [...document.querySelectorAll("button,[role=button],a")]
      .map((el) => ({ text: el.matches(MINE) ? "[redacted]" : text(el),
                      auto: el.getAttribute("data-automation-id") || "",
                      disabled: !!el.disabled }))
      .filter((b) => b.text).slice(0, 40);

  // The page, with the person taken out of it. Done on a clone so the live
  // page is untouched - this tool may not change what it is watching.
  const copy = document.documentElement.cloneNode(true);
  for (const el of copy.querySelectorAll("input")) {
    if (el.getAttribute("type") === "checkbox" || el.getAttribute("type") === "radio") continue;
    if (el.hasAttribute("value")) el.setAttribute("value", "[redacted]");
  }
  for (const el of copy.querySelectorAll("textarea")) el.textContent = "[redacted]";
  // Workday renders a chosen value as a pill and a file as a row; both are
  // the person's. The control stays, its content goes.
  for (const el of copy.querySelectorAll("[data-automation-id='selectedItem'],"
      + "[data-automation-id='file-upload-item-name'],[data-automation-id='promptAriaInstruction'],"
      // The signed-in account's own email, in the header of every page.
      + "#accountSettingsButton,[data-automation-id='ariaLiveMessage']"))
    el.textContent = "[redacted]";
  // The person is in more than the values. A filename is named after them
  // and Workday repeats it in the bin's aria-label and the live region; the
  // account header carries their email as an aria-label, which is not text
  // and so survived being emptied; and an input's placeholder on an account
  // page is often the value it wants back. Each of the four attributes is
  // read for an email, a filename and a phone-shaped number (2026-10-26,
  // found by grepping a login page for the maintainer's own address - the
  // only test of a scrubber worth trusting, 2026-10-11).
  //
  // A fresh regex per use, deliberately: a `/g` regex reused across
  // `.test()` calls keeps its `lastIndex`, so every other match was missed.
  const CARRIES = [
    () => /[^\s"'<>@]+@[^\s"'<>@]+\.[a-z]{2,}/gi,            // an address
    () => /[^\s"'<>]+\.(pdf|docx?|rtf|txt|odt|pages)\b/gi,    // a document
    () => /\+?\d[\d().\-\s]{7,}\d/g,                        // a number to ring
  ];
  const ATTRS = ["aria-label", "title", "alt", "placeholder", "value"];
  // Anything naming the person outright: the header, the live region, and
  // the pills and rows above. Their attributes go as well as their text.
  const THEIRS = "[data-automation-id='selectedItem'],#accountSettingsButton,"
      + "[data-automation-id='ariaLiveMessage'],[data-automation-id='file-upload-item-name']";
  for (const el of copy.querySelectorAll("*")) {
    const named = el.matches(THEIRS);
    for (const name of ATTRS) {
      let was = el.getAttribute(name);
      if (!was) continue;
      if (named) { el.setAttribute(name, "[redacted]"); continue; }
      for (const make of CARRIES) was = was.replace(make(), "[redacted]");
      el.setAttribute(name, was);
    }
  }
  // "Welcome, <name>" is the employer's word and the person's name in one
  // string. The greeting is the shape; the name is not.
  for (const el of copy.querySelectorAll("h1,h2,h3,h4,span,div,p")) {
    if (el.children.length) continue;
    const was = (el.textContent || "").trim();
    const hello = was.match(/^(welcome|hi|hello|signed in as|logged in as)\b/i);
    if (hello && was.length > hello[1].length + 1) el.textContent = hello[1] + " [redacted]";
  }
  for (const el of copy.querySelectorAll("script,style,svg,img,iframe")) el.remove();

  return JSON.stringify({
    url: location.href.split("?")[0], title: document.title, heads: heads,
    buttons: buttons, fields: fields.length,
    files: document.querySelectorAll("input[type=file]").length,
    passwords: document.querySelectorAll("input[type=password]").length,
    html: copy.outerHTML,
  });
})()
"""


# What no pattern can find: a person's name. The scrubber above works on the
# shape of a thing - an address, a filename, a number - and a name has none.
# So the last pass is literal: whatever the profile on this machine says the
# person is called is replaced wherever it still appears. Belt over braces,
# and it is the pass that would have caught the five fixtures committed with
# the maintainer's resume filename in them (2026-10-11).
NEEDLES: list[str] = []


def needles() -> list[str]:
    """The person's own words, longest first so a full name goes before the
    halves of it. Read once, from the profile the fill already uses."""
    if NEEDLES:
        return NEEDLES
    found: set[str] = set()
    form = paths.BASE / "form.json"
    try:
        answers = json.loads(form.read_text()) if form.exists() else {}
    except (OSError, ValueError):
        answers = {}
    for key, value in (answers.get("answers") or answers).items():
        if not isinstance(value, str) or len(value.strip()) < 3:
            continue
        if any(word in key.lower() for word in
               ("name", "email", "phone", "address", "city", "linkedin", "github", "website")):
            found.add(value.strip())
    for whole in list(found):
        # A full name is also its parts: Workday shows "First" and "Last" in
        # separate places and only one of them may be left behind.
        if " " in whole and "@" not in whole and len(whole) < 60:
            found.update(part for part in whole.split() if len(part) > 2)
    NEEDLES.extend(sorted(found, key=len, reverse=True))
    return NEEDLES


def scrub(html: str) -> tuple[str, list[str]]:
    """The page with every needle taken out, and which ones were there. A
    needle that is found is a hole in `SNAPSHOT_JS`, so it is said out loud
    rather than quietly patched over."""
    left = []
    for needle in needles():
        if needle.lower() in html.lower():
            left.append(needle if "@" not in needle else "an address")
            pattern = re.compile(re.escape(needle), re.I)
            html = pattern.sub("[redacted]", html)
    return html, left


def shape(snap: dict) -> tuple:
    """What makes this page a different page from the last one."""
    return (snap.get("url"), tuple(snap.get("heads") or []), snap.get("fields"),
            snap.get("files"), snap.get("passwords"))


class Session:
    """One Workday tab, watched. Its own directory, its own flow file."""

    def __init__(self, target_id: str, url: str, root: Path):
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
        self.dir = root / f"{stamp}_{target_id[:8]}"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.target_id = target_id
        self.flow = (self.dir / "flow.jsonl").open("a", encoding="utf-8")
        self.pages: list[dict] = []
        self.last: Optional[tuple] = None
        self.moves = 0
        self.write({"kind": "session", "url": url.split("?")[0],
                    "at": int(time.time() * 1000), "recorder": "dev_workday_recorder"})

    def write(self, row: dict) -> None:
        """Every row through the needle pass, not only the saved pages. A
        heading, a button's text and a page title all carry the person's name
        on an account page, and the flow is the file that gets read."""
        line, left = scrub(json.dumps(row, ensure_ascii=False))
        if left:
            print(f"  ! {', '.join(left)} was in the flow and has been taken out")
        self.flow.write(line + "\n")
        self.flow.flush()

    def page(self, snap: dict) -> None:
        """A page we have not seen the shape of before."""
        number = len(self.pages) + 1
        name = f"page_{number:02d}.html"
        html, left = scrub(snap.pop("html", ""))
        if left:
            print(f"  ! page {number}: {', '.join(left)} was still in the page and "
                  f"has been taken out")
        (self.dir / name).write_text(html, encoding="utf-8")
        row = {"kind": "page", "number": number, "file": name,
               "at": int(time.time() * 1000), "stage": workday.stage_of(snap),
               "url": snap.get("url"), "title": snap.get("title"),
               "heads": snap.get("heads"), "buttons": snap.get("buttons"),
               "fields": snap.get("fields"), "files": snap.get("files"),
               "passwords": snap.get("passwords")}
        self.pages.append(row)
        self.write(row)
        print(f"  page {number:>2}  {row['stage']:<8} {(row['heads'] or [''])[0][:54]}")

    def close(self) -> None:
        (self.dir / "pages.json").write_text(
            json.dumps({"pages": self.pages, "moves": self.moves}, indent=2), encoding="utf-8")
        self.flow.close()


def arm_only_on_workday() -> str:
    """`Page.addScriptToEvaluateOnNewDocument` fires on **every** document the
    tab goes on to show, and `recordable` was asked once, before the tab
    existed. So a tab that left Workday for a university's single sign-on,
    a Duo prompt and a Google account chooser had the listener installed on
    each of them and was snapshotted there too - seven pages of somebody
    else's sign-in saved on 2026-10-13, one of them carrying the
    maintainer's own address, found on 2026-10-29 by asking the recordings
    what host each page was on.

    The listener now refuses to arm unless the document it is on is one this
    tool may watch, which is the check at the source. `follow` asks the same
    question of the live URL every poll, which is the check at the drain.
    Two halves, because the first cannot see a same-document route change
    and the second cannot stop a script that is already running.
    """
    # The host is matched as a host, not as a substring of the whole URL: a
    # path carrying `myworkdayjobs.com` is not a Workday tenant. Only the
    # auth entries, which name a path (`myworkday.com/wday/authgwy`), and
    # `--match` are read loosely.
    hosts = json.dumps([h.lower().lstrip(".") for h in workday.HOSTS if h])
    loose = json.dumps([h.lower() for h in list(WORKDAY_AUTH) + list(EXTRA_HOSTS) if h])
    return ("(() => { const host = location.host.toLowerCase();"
            " const whole = (host + location.pathname).toLowerCase();"
            f" const ok = {hosts}.some((h) => host === h || host.endsWith('.' + h))"
            f"   || {loose}.some((h) => whole.includes(h));"
            " if (!ok) return;")


async def follow(cdp_url: str, target_id: str, url: str, root: Path) -> None:
    """Watch one tab until it goes away."""
    session = Session(target_id, url, root)
    print(f"recording {url.split('?')[0][:80]}\n  -> {session.dir}")
    try:
        async with attached(cdp_url, target_id=target_id) as (page, _):
            # On this document and on every one after it: a Workday step is
            # not a navigation, but a sign-in round trip is.
            await page.send("Page.enable")
            armed = f"{arm_only_on_workday()}try{{{RECORDER_JS}}}catch(e){{}} }})()"
            await page.send("Page.addScriptToEvaluateOnNewDocument", {"source": armed})
            await page.evaluate(armed)
            since = 0.0
            away = False
            while True:
                here = str(await page.evaluate("location.href") or "")
                if here and not recordable(here):
                    # Somebody else's sign-on. Nothing is drained and nothing
                    # is snapshotted until the tab is back on the employer's
                    # own pages; that it happened is the only fact kept.
                    if not away:
                        away = True
                        session.write({"kind": "away", "at": int(time.time() * 1000),
                                       "host": (here.split("/") + ["", "", ""])[2]})
                        print("  away    (not a Workday page; not recording)")
                    await asyncio.sleep(POLL)
                    continue
                if away:
                    away = False
                    session.last = None       # the page it comes back on is news
                    print("  back    on Workday")
                raw = await page.evaluate(DRAIN_JS)
                for row in json.loads(raw or "[]"):
                    session.moves += 1
                    session.write(row)
                    el = row.get("el") or {}
                    said = el.get("label") or el.get("says") or el.get("auto") or el.get("tag")
                    extra = (row.get("shape") or row.get("chose")
                             or (", ".join(row.get("files") or []) if row.get("files") else "")
                             or ("on" if row.get("on") else ""))
                    print(f"  {row['kind']:<7} {str(said)[:44]:<44} {extra}")
                if time.monotonic() - since >= SHAPE_POLL:
                    since = time.monotonic()
                    snap = json.loads(await page.evaluate(SNAPSHOT_JS) or "{}")
                    if snap:
                        now = shape(snap)
                        if now != session.last:
                            session.last = now
                            await page.evaluate(RECORDER_JS)  # survives a remount
                            session.page(snap)
                await asyncio.sleep(POLL)
    except Exception as error:  # noqa: BLE001 - the tab closed, or Chrome did
        session.write({"kind": "end", "why": str(error)[:200], "at": int(time.time() * 1000)})
    finally:
        session.close()
        print(f"stopped: {session.dir.name} — {len(session.pages)} page(s), {session.moves} move(s)")


# Hosts to record beyond Workday's own, for testing this tool against a
# page of our own making (`--match`). Empty in normal use.
EXTRA_HOSTS: list[str] = []


# Workday's own account pages. `signin.IDENTITY_HOSTS` lumps these in with
# Google, Microsoft and Okta, which is right for the live app - a tab that
# has left for somebody's single sign-on is on a detour and the credential
# there is nothing to do with the employer. It was wrong for the recorder:
# **the Workday account is part of the application**, the step the walk stops
# at more than any other, and a page that is never recorded is a page the
# replay cannot show. Third-party sign-on stays excluded; Workday's own does
# not. Nothing is read either way that a password could hide in - a password
# input is `<secret>` with no length, and its value is not even measured.
WORKDAY_AUTH = ("id.workday.com", "authn.workday.com", "myworkday.com/wday/authgwy")


def workday_own_account(url: str) -> bool:
    low = (url or "").lower()
    return any(h in low for h in WORKDAY_AUTH)


def recordable(url: str) -> bool:
    """A Workday page, including Workday's own account and sign-in pages, and
    never somebody else's single sign-on."""
    if not url:
        return False
    low = url.lower()
    if workday_own_account(url):
        return True
    if signin.identity_host(url):
        return False
    return workday.is_workday(url) or any(h and h in low for h in EXTRA_HOSTS)


async def watch(cdp_url: str, root: Path) -> None:
    """Attach to every Workday tab that appears, once each."""
    following: dict[str, asyncio.Task] = {}
    print("watching for Workday tabs… (ctrl-C to stop)\n")
    while True:
        try:
            tabs = _tabs(cdp_url)
        except Exception as error:  # noqa: BLE001 - Chrome went away
            print(f"chrome: {error}")
            await asyncio.sleep(3)
            continue
        for tab in tabs:
            target_id, url = tab.get("id") or "", tab.get("url") or ""
            if not target_id or target_id in following or not recordable(url):
                continue
            following[target_id] = asyncio.create_task(follow(cdp_url, target_id, url, root))
        for target_id in [k for k, t in following.items() if t.done()]:
            following.pop(target_id, None)
        await asyncio.sleep(2)


def _tabs(cdp_url: str) -> list[dict]:
    import urllib.request
    with urllib.request.urlopen(f"{cdp_url}/json", timeout=5) as answer:
        return [t for t in json.loads(answer.read()) if t.get("type") == "page"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=0, help="Chrome's debugging port")
    parser.add_argument("--out", default="", help="where the recordings go")
    parser.add_argument("--match", action="append", default=[],
                        help="also record tabs whose URL contains this (for testing the "
                             "recorder itself, or a Workday tenant on an odd host)")
    parser.add_argument("--anyway", action="store_true",
                        help="run past the expiry date (say why to yourself first)")
    args = parser.parse_args()

    today = datetime.now().date()
    if today > EXPIRES and not args.anyway:
        print(f"This recorder was for {EXPIRES:%d %B %Y} and today is {today:%d %B %Y}.\n"
              f"Delete tools/dev_workday_recorder.py and tools/dev_workday_replay.py,\n"
              f"or move EXPIRES if there is a new reason to record.", file=sys.stderr)
        return 2

    EXTRA_HOSTS.extend(h.lower() for h in args.match)
    port = args.port or chrome.port()
    cdp_url = chrome.cdp_url(port)
    root = Path(args.out) if args.out else OUT
    root.mkdir(parents=True, exist_ok=True)

    print(f"dev Workday recorder — shape only, no values, expires {EXPIRES:%d %B}")
    print(f"chrome {cdp_url}   ->  {root}")
    try:
        asyncio.run(watch(cdp_url, root))
    except KeyboardInterrupt:
        print("\nstopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
