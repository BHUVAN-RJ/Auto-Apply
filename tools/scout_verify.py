"""Can the scout read every watched careers page, and does each list
roles at the applicant's level?

    python tools/scout_verify.py                 # every watch in data/scout.json
    python tools/scout_verify.py <url> [<url>…]  # these pages, without saving a watch
    python tools/scout_verify.py --json          # machine-readable

One line per page: OK with the counts and a few matching titles, or
BROKEN in red with the reason. Exit status is the number of broken
pages, so a cron or a friend's shell can see it. Nothing is written
except the watch's own `error` field when a saved watch is checked,
which is what the page's red banner reads.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scout import DetectError, Watch, detect, run, store  # noqa: E402

RED = "\033[1;97;41m"
GREEN = "\033[1;32m"
DIM = "\033[2m"
END = "\033[0m"


def watches_for(urls: list[str]) -> list[tuple[Watch, bool]]:
    """(watch, saved) per URL; the whole file when no URL is given."""
    if not urls:
        return [(w, True) for w in store.watches()]
    out = []
    for url in urls:
        try:
            provider, args, company = detect(url)
        except DetectError as exc:
            out.append((Watch(url=url, company=url, provider="", error=str(exc)), False))
            continue
        out.append((Watch(url=url, company=company, provider=provider, args=args, query=args.pop("query", "")), False))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("urls", nargs="*")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-color", action="store_true")
    args = parser.parse_args(argv)
    colour = not args.no_color and sys.stdout.isatty() and not args.json

    rows = []
    for watch, saved in watches_for(args.urls):
        if not watch.provider:
            result = {"ok": False, "error": watch.error, "total": 0, "matching": 0, "sample": [], "seconds": 0}
        else:
            result = run.verify(watch)
            if saved:
                store.update_watch(watch.id, error=None if result["ok"] else result["error"])
        rows.append({"company": watch.company, "provider": watch.provider or "-", "url": watch.url, **result})

    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        if not rows:
            print("No watches. Add one on the Scout tab, or pass a careers URL.")
        for r in rows:
            name = f"{r['company']} ({r['provider']})"
            if r["ok"]:
                tag = f"{GREEN}OK{END}" if colour else "OK"
                level = f"{r['matching']} at your level of {r['total']}" if r["matching"] else f"none at your level of {r['total']} (filter?)"
                print(f"{tag:<8} {name:<32} {level}  {DIM if colour else ''}{r['seconds']}s{END if colour else ''}")
                for title in r["sample"][:3]:
                    print(f"         · {title}")
            else:
                tag = f"{RED} BROKEN {END}" if colour else "BROKEN"
                print(f"{tag} {name:<32} {r['error']}")
                print(f"         {r['url']}")
    return sum(1 for r in rows if not r["ok"])


if __name__ == "__main__":
    sys.exit(main())
