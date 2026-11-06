"""Where a tracking session lands, and the running tally it keeps.

One directory per run: `flow.jsonl` is every row in the order it happened,
`pages/` holds the scrubbed HTML of each distinct page shape, and
`summary.json` is the counts - which is what gets read first when the
question is "what happened in that hour".

The scrub is the one thing in here that is not bookkeeping. A keystroke log
is the person's own and keeps what they typed; a saved page is a fixture we
may want to keep, and a fixture carrying a name has been committed to a
public repository once already.
"""
from __future__ import annotations

import json
import subprocess
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import paths

from . import policy

TEXT_CAP = 400_000     # of one page's HTML
# Cloning a whole document and serialising it runs synchronously in the page's
# own renderer. On a big single-page app that is not a cheap read: it starved
# jobright.ai badly enough that the page stopped loading (2026-10-29). So a
# page over this many elements is counted and described, never cloned.
BIG = 12_000           # elements, over which a page is too big to save


def _answers() -> dict:
    form = paths.BASE / "form.json"
    try:
        raw = json.loads(form.read_text()) if form.exists() else {}
    except (OSError, ValueError):
        return {}
    return raw.get("answers") or raw


def build() -> dict:
    """The commit the watched code was at, and whether it was edited.

    Nothing personal: a short hash and a dirty flag. Git missing or this not
    being a checkout is not an error - the tracker's job is to keep watching.
    """
    def git(*args: str) -> str:
        try:
            out = subprocess.run(("git", *args), cwd=paths.ROOT, capture_output=True,
                                 text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return ""
        return out.stdout.strip() if out.returncode == 0 else ""

    commit = git("rev-parse", "--short", "HEAD")
    return {"commit": commit,
            "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
            "dirty": bool(git("status", "--porcelain")) if commit else False}


class Session:
    def __init__(self, root: Path, needles: Optional[list[str]] = None):
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
        self.dir = root / stamp
        (self.dir / "pages").mkdir(parents=True, exist_ok=True)
        self.flow = (self.dir / "flow.jsonl").open("a", encoding="utf-8")
        self.needles = needles if needles is not None else policy.needles(_answers())
        self.tally: Counter = Counter()
        self.shapes: dict[str, int] = {}
        self.started = time.time()
        self.leaks: Counter = Counter()
        self.build = build()
        self.write({"kind": "session", "at": int(time.time() * 1000),
                    "tracker": "tracking.collector",
                    "build": self.build,
                    "needles": len(self.needles)})
        self.mark()

    def mark(self) -> None:
        """Which build this session watched, written the moment it starts.

        A tracking run outlives the code it is watching: the live session of
        2026-10-30 spans the day the countdown was taken out, so reading it
        means knowing where the cut is. A session that says its own commit at
        the top needs no such archaeology, and it is written at the start
        rather than at `close`, because a run killed with ^C never closes.
        """
        (self.dir / "mark.json").write_text(json.dumps({
            "session": self.dir.name,
            "started": datetime.fromtimestamp(self.started, timezone.utc).isoformat(),
            "build": self.build,
            "live": True,
        }, indent=2) + "\n", encoding="utf-8")

    # --- writing ------------------------------------------------------

    def write(self, row: dict) -> None:
        row.setdefault("at", int(time.time() * 1000))
        line, found = policy.scrub(json.dumps(row, ensure_ascii=False), self.needles)
        for needle in found:
            self.leaks[needle] += 1
        self.flow.write(line + "\n")
        self.flow.flush()

    def count(self, row: dict) -> None:
        """The tally the summary is built from. Deliberately about the
        person's own actions: a script's click is not a decision."""
        kind, trusted = row.get("kind"), bool(row.get("trusted"))
        self.tally[kind] += 1
        self.tally[f"{kind}:{'mine' if trusted else 'script'}"] += 1
        if kind == "edit":
            self.tally["edits"] += 1
            if row.get("corrected"):
                self.tally["corrections of an autofill"] += 1
        if kind == "error":
            self.tally[f"errors {row.get('what')}"] += 1
        if kind == "window.open":
            self.tally["window.open calls"] += 1
        if kind == "window.close":
            self.tally["window.close calls"] += 1

    async def snapshot(self, page, tab: str, fields: list[dict]) -> Optional[tuple]:
        """The page as it is now, saved once per distinct shape. Returns the
        shape, so the caller can tell a new page from a redraw."""
        try:
            title = str(await page.evaluate("document.title") or "")
            url = str(await page.evaluate("location.href") or "")
            heads = json.loads(await page.evaluate(
                "JSON.stringify([...document.querySelectorAll('h1,h2,h3,h4')]"
                ".map(h => (h.innerText||'').replace(/\\s+/g,' ').trim()).filter(Boolean).slice(0,8))")
                or "[]")
        except Exception:  # noqa: BLE001 - the tab went away mid-look
            return None
        kinds = Counter(f.get("type") or f.get("tag") for f in fields)
        # Deliberately not the headings: on a job board they are the listings,
        # so they change on every scroll and every filter, and a shape that
        # changes on a scroll means a full clone of the page on every scroll.
        shape = (url.split("?")[0], len(fields), tuple(sorted(kinds.items())))
        key = json.dumps(shape)
        if key in self.shapes:
            return shape
        number = len(self.shapes) + 1
        self.shapes[key] = number
        try:
            elements = int(await page.evaluate("document.querySelectorAll('*').length") or 0)
        except Exception:  # noqa: BLE001
            elements = 0
        if elements > BIG:
            self.write({"kind": "page", "tab": tab, "number": number, "file": "",
                        "url": url, "title": title[:160], "heads": heads,
                        "fields": len(fields), "kinds": dict(kinds),
                        "elements": elements, "not_saved": "too big to clone"})
            return shape
        try:
            html = str(await page.evaluate(VALUES_OUT) or "")
        except Exception:  # noqa: BLE001
            html = ""
        # A saved page is a fixture: shapes as well as needles.
        clean, found = policy.scrub(html[:TEXT_CAP], self.needles, shapes=True)
        for needle in found:
            self.leaks[needle] += 1
        name = f"page_{number:03d}.html"
        (self.dir / "pages" / name).write_text(clean, encoding="utf-8")
        self.write({"kind": "page", "tab": tab, "number": number, "file": f"pages/{name}",
                    "url": url, "title": title[:160], "heads": heads,
                    "fields": len(fields), "kinds": dict(kinds),
                    "needles_found": found})
        if found:
            print(f"  ! {', '.join(found)} was still in page {number}; taken out")
        return shape

    def close(self) -> None:
        summary = {
            "started": datetime.fromtimestamp(self.started, timezone.utc).isoformat(),
            "seconds": round(time.time() - self.started),
            "pages": len(self.shapes),
            "tally": dict(sorted(self.tally.items())),
            "needles_caught": dict(self.leaks),
        }
        (self.dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        self.flow.close()
        print(f"\nstopped: {self.dir.name} — {len(self.shapes)} page shape(s)")
        for key in ("click:mine", "key:mine", "edits", "corrections of an autofill",
                    "errors raised", "errors cleared", "window.open calls"):
            if self.tally.get(key):
                print(f"  {key:<28} {self.tally[key]}")


# The page with every value taken out of the markup. The keystroke log keeps
# what was typed; a saved page is a fixture, and a fixture carries nobody's
# details. Done on a clone, because this tool may not change what it watches.
VALUES_OUT = r"""
(() => {
  const copy = document.documentElement.cloneNode(true);
  for (const el of copy.querySelectorAll("input")) {
    const type = (el.getAttribute("type") || "").toLowerCase();
    if (type === "checkbox" || type === "radio") continue;
    if (el.hasAttribute("value")) el.setAttribute("value", "[redacted]");
  }
  for (const el of copy.querySelectorAll("textarea")) el.textContent = "[redacted]";
  const MINE = "[data-automation-id='selectedItem'],#accountSettingsButton,"
      + "[data-automation-id='ariaLiveMessage'],[data-automation-id='file-upload-item-name']";
  for (const el of copy.querySelectorAll(MINE)) {
    el.textContent = "[redacted]";
    for (const name of ["aria-label", "title", "alt", "placeholder", "value"])
      if (el.hasAttribute(name)) el.setAttribute(name, "[redacted]");
  }
  for (const el of copy.querySelectorAll("h1,h2,h3,h4,span,div,p")) {
    if (el.children.length) continue;
    const was = (el.textContent || "").trim();
    const hello = was.match(/^(welcome|hi|hello|signed in as|logged in as)\b/i);
    if (hello && was.length > hello[1].length + 1) el.textContent = hello[1] + " [redacted]";
  }
  for (const el of copy.querySelectorAll("script,style,svg,img,iframe,video,audio")) el.remove();
  return copy.outerHTML;
})()
"""
