"""A tracking session, read back in English.

`python -m tracking.report` for the newest, or a directory for one of them.
The questions it answers are the ones the session was started to answer:
how much did I change, what errors did I fix, where did the blank tabs come
from, did I close them, and how did our own table score.
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from typing import Optional

import paths


def rows(session: Path) -> list[dict]:
    flow = session / "flow.jsonl"
    out = []
    for line in flow.read_text(errors="replace").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def newest(root: Optional[Path] = None) -> Optional[Path]:
    base = root or (paths.DATA / "tracking")
    runs = sorted((d for d in base.glob("*") if (d / "flow.jsonl").exists()),
                  key=lambda d: d.name)
    return runs[-1] if runs else None


def report(session: Path) -> str:
    out: list[str] = [f"# {session.name}"]
    data = rows(session)
    kinds = Counter(r.get("kind") for r in data)
    mine = Counter(r.get("kind") for r in data if r.get("trusted") is True)
    script = Counter(r.get("kind") for r in data if r.get("trusted") is False)

    out.append(f"\n{len(data)} rows, {kinds.get('page', 0)} page shapes, "
               f"{kinds.get('tab_opened', 0)} tabs opened, {kinds.get('tab_closed', 0)} closed")

    out.append("\n## My hand vs a script")
    for kind in ("click", "key", "input", "change", "edit", "submit"):
        if kinds.get(kind):
            out.append(f"  {kind:<8} mine {mine.get(kind, 0):>6}   script {script.get(kind, 0):>6}")

    edits = [r for r in data if r.get("kind") == "edit"]
    fixed = [r for r in edits if r.get("corrected")]
    out.append(f"\n## Changes\n  {len(edits)} field(s) left with a different value than they "
               f"were entered with\n  {len(fixed)} of those were over something a script had put there")
    for r in fixed[:12]:
        label = ((r.get("el") or {}).get("label") or (r.get("el") or {}).get("auto") or "?")[:46]
        out.append(f"    {label:<46} {str(r.get('filled'))[:28]!r} -> {str(r.get('now'))[:28]!r}")

    raised = [r for r in data if r.get("kind") == "error" and r.get("what") == "raised"]
    cleared = [r for r in data if r.get("kind") == "error" and r.get("what") == "cleared"]
    out.append(f"\n## Errors\n  {len(raised)} raised, {len(cleared)} cleared")
    says = Counter(r.get("says", "")[:70] for r in raised)
    for text, n in says.most_common(10):
        out.append(f"    {n:>3}x {text}")

    blanks = [r for r in data if r.get("kind") == "tab_opened" and r.get("blank")]
    out.append(f"\n## Tabs that opened blank: {len(blanks)}")
    for r in blanks[:20]:
        because = r.get("because") or {}
        touch = (because.get("after") or {}).get("el") or {}
        out.append(f"    tab {r.get('tab')}  opener={r.get('opener') or '-'}  "
                   f"{'asked for ' + (because.get('wanted') or '(nothing)') if because else 'no window.open seen'}")
        if touch:
            out.append(f"        last touched: {(touch.get('label') or touch.get('says') or touch.get('auto') or touch.get('tag'))[:60]!r}"
                       f"  {because.get('sinceTouch')}ms before")
        if because.get("from"):
            out.append(f"        called from: {because['from'][:150]}")

    closed = [r for r in data if r.get("kind") == "tab_closed"]
    itself = [r for r in closed if r.get("closed_itself")]
    out.append(f"\n## Who closed tabs\n  {len(itself)} closed themselves (window.close from in the page)"
               f"\n  {len(closed) - len(itself)} were closed from outside the page (a hand, or an extension)")
    blank_closed = [r for r in closed if r.get("blank")]
    out.append(f"  of all closures, {len(blank_closed)} were blank tabs")

    shadows = [r for r in data if r.get("kind") == "shadow"]
    if shadows:
        out.append(f"\n## Our own table, run in parallel ({len(shadows)} page(s))")
        for r in shadows[-12:]:
            out.append(f"    {r.get('system'):<9} {r.get('score'):<7} "
                       f"missed: {', '.join(r.get('cannot_see') or []) or '-'}")
        miss = Counter(k for r in shadows for k in (r.get("cannot_see") or []))
        if miss:
            out.append("  fields the table never resolved:")
            for key, n in miss.most_common(12):
                out.append(f"    {n:>3}x {key}")
        broken = Counter(k for r in shadows for k in (r.get("broken") or {}))
        if broken:
            out.append(f"  selectors that do not compile: {dict(broken)}")

    errors = [r for r in data if r.get("kind") == "page_error"]
    if errors:
        out.append(f"\n## The page's own complaints: {len(errors)}")
        for text, n in Counter(r.get("says", "")[:80] for r in errors).most_common(6):
            out.append(f"    {n:>3}x {text}")

    summary = session / "summary.json"
    if summary.exists():
        out.append(f"\n## Needles caught by the scrub\n  "
                   f"{json.loads(summary.read_text()).get('needles_caught') or 'none'}")
    return "\n".join(out)


def main(argv: Optional[list[str]] = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    session = Path(args[0]) if args else newest()
    if session is None or not (session / "flow.jsonl").exists():
        print("no tracking session found", file=sys.stderr)
        return 2
    print(report(session))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
