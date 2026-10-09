"""DEVELOPER TOOL — serves a recorded Workday application back, locally.

**Deletable, with its recorder.** `rm tools/dev_workday_recorder.py
tools/dev_workday_replay.py` and the feature is gone; nothing imports
either.

What it is for: `dev_workday_recorder.py` keeps the pages of a real Workday
application (values scrubbed) and the moves the person made on them. This
serves those pages on `127.0.0.1` so the real `browser/` code can be run
against them - `FIND_REMOVE_FN` against the real nesting, `stage_of` against
the real headings, `workday.walk` against the real sequence - without a
Workday account, without an employer, and without waiting ten minutes for a
sign-in. A local Workday to develop against, which is what the last three
bugs each needed and none of them had.

    uv run python -m tools.dev_workday_replay                  # newest session
    uv run python -m tools.dev_workday_replay --list
    uv run python -m tools.dev_workday_replay --session 2026-10-09T18-02-11_A1B2C3D4

Then:

    http://127.0.0.1:8799/            the flow, page by page, as a list
    http://127.0.0.1:8799/page_03     one page, served as itself
    http://127.0.0.1:8799/flow.json   the moves, as recorded

A page served here is the employer's markup with the person's answers taken
out of it. It is a fixture, not an application: nothing on it submits
anywhere, and the forms have no action.
"""

from __future__ import annotations

import argparse
import json
import sys
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import paths  # noqa: E402

OUT = paths.DATA / "dev_recordings" / "workday"
PORT = 8799   # not 8787: the app is probably running


def sessions(root: Path) -> list[Path]:
    return sorted((d for d in root.glob("*") if d.is_dir() and (d / "flow.jsonl").exists()),
                  key=lambda d: d.name)


def flow(session: Path) -> list[dict]:
    rows = []
    for line in (session / "flow.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows


def summarise(session: Path) -> str:
    """The flow in one screen: what the person met, and what they did on it."""
    rows = flow(session)
    pages = [r for r in rows if r.get("kind") == "page"]
    out = [f"{session.name}  —  {len(pages)} page(s), "
           f"{sum(1 for r in rows if r.get('kind') not in ('page', 'session', 'end'))} move(s)"]
    for row in rows:
        kind = row.get("kind")
        if kind == "page":
            out.append("")
            out.append(f"  ── page {row.get('number'):>2}  [{row.get('stage')}]  "
                       f"{(row.get('heads') or [''])[0][:60]}")
            out.append(f"     {row.get('fields')} field(s), {row.get('files')} file slot(s), "
                       f"{row.get('passwords')} password box(es)   {row.get('file')}")
        elif kind in ("click", "type", "choose", "toggle", "file", "leave"):
            el = row.get("el") or {}
            who = el.get("label") or el.get("says") or el.get("aria") or el.get("auto") or el.get("tag") or "?"
            what = (row.get("shape") or row.get("chose")
                    or (", ".join(row.get("files") or []) if row.get("files") else "")
                    or ("on" if row.get("on") else "off" if kind == "toggle" else ""))
            coded = el.get("auto") or el.get("id") or ""
            out.append(f"     {kind:<7} {str(who)[:40]:<40} {str(what)[:24]:<24} {coded[:28]}")
        elif kind == "end":
            out.append(f"  (ended: {row.get('why', '')[:70]})")
    return "\n".join(out)


INDEX = """<!doctype html><meta charset=utf-8><title>Recorded Workday flow</title>
<style>body{font:13px ui-monospace,monospace;margin:24px;max-width:900px}
a{color:#6a00ff}h1{font-size:15px}pre{white-space:pre-wrap;line-height:1.5}
li{margin:.3em 0}</style>
<h1>%(name)s</h1>
<p>%(count)d page(s). The employer's markup, with the applicant's answers removed.</p>
<ul>%(links)s</ul>
<h1>The flow as recorded</h1>
<pre>%(flow)s</pre>
"""


class Replay(SimpleHTTPRequestHandler):
    """Serves one session: its pages by name, its flow as JSON, an index."""

    session: Path

    def do_GET(self) -> None:  # noqa: N802 - the base class's name
        want = self.path.split("?")[0].strip("/")
        if not want:
            return self._html(self._index())
        if want == "flow.json":
            return self._send(json.dumps(flow(self.session), indent=1).encode(),
                              "application/json")
        name = want if want.endswith(".html") else f"{want}.html"
        page = self.session / name
        if page.name != name or not page.exists():   # no climbing out
            self.send_error(404, "no such page in this recording")
            return
        return self._send(page.read_bytes(), "text/html; charset=utf-8")

    def _index(self) -> str:
        rows = [r for r in flow(self.session) if r.get("kind") == "page"]
        links = "".join(
            f'<li><a href="/{r["file"][:-5]}">{r["file"]}</a> — [{r.get("stage")}] '
            f'{(r.get("heads") or [""])[0][:70]}</li>' for r in rows)
        import html
        return INDEX % {"name": self.session.name, "count": len(rows), "links": links,
                        "flow": html.escape(summarise(self.session))}

    def _html(self, body: str) -> None:
        self._send(body.encode(), "text/html; charset=utf-8")

    def _send(self, body: bytes, kind: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        pass   # the flow is the interesting output, not the requests


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--session", default="", help="which recording (default: newest)")
    parser.add_argument("--list", action="store_true", help="list the recordings and stop")
    parser.add_argument("--print", action="store_true", help="print the flow and stop")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--out", default="", help="where the recordings are")
    args = parser.parse_args()

    root = Path(args.out) if args.out else OUT
    found = sessions(root)
    if not found:
        print(f"no recordings in {root}\nrun: uv run python -m tools.dev_workday_recorder",
              file=sys.stderr)
        return 2

    if args.list:
        for session in found:
            pages = [r for r in flow(session) if r.get("kind") == "page"]
            stages = " → ".join(dict.fromkeys(r.get("stage", "?") for r in pages))
            print(f"{session.name}  {len(pages):>2} page(s)  {stages}")
        return 0

    chosen: Optional[Path] = None
    if args.session:
        chosen = next((s for s in found if s.name == args.session or args.session in s.name), None)
        if chosen is None:
            print(f"no recording called {args.session!r}", file=sys.stderr)
            return 2
    else:
        chosen = found[-1]

    if args.print:
        print(summarise(chosen))
        return 0

    Replay.session = chosen
    server = HTTPServer(("127.0.0.1", args.port), partial(Replay))
    print(summarise(chosen))
    print(f"\nserving {chosen.name} on http://127.0.0.1:{args.port}/   (ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
