"""The ladder from a terminal.

    python -m identity backfill
    python -m identity check <url> [--title T] [--company C] [--location L]
    python -m identity explain <url> [...]
    python -m identity stats
    python -m identity collisions

`check` exits 2 on BLOCK, 1 on WARN and 0 on CLEAR, so a shell can use it
as a gate.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

import identity


def _probe_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("url")
    parser.add_argument("--title", default="")
    parser.add_argument("--company", default="")
    parser.add_argument("--location", default="")
    parser.add_argument("--text", default="", help="the posting text, for the simhash rule")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m identity", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("backfill", help="build the index from the queue and applications/")
    _probe_args(sub.add_parser("check", help="the verdict for one posting"))
    _probe_args(sub.add_parser("explain", help="the verdict and every key it was looked up by"))
    sub.add_parser("stats", help="what the index holds, and whether it is behind")
    sub.add_parser("collisions", help="four-hex job ids that name more than one job")
    args = parser.parse_args(argv)

    if args.command == "backfill":
        started = time.monotonic()
        report = identity.backfill()
        report["seconds"] = round(time.monotonic() - started, 2)
        print(json.dumps(report, indent=2))
        return 0

    if args.command == "stats":
        print(json.dumps(identity.stats(), indent=2))
        return 0

    if args.command == "collisions":
        found = identity.collisions()
        if not found:
            print("No four-hex id in the data names more than one job.")
            return 0
        for display_id, group in found.items():
            print(f"{display_id}: {len(group)} jobs")
            for row in group:
                print(f"  {row['key']}  {row['status'] or '?':16} {row['url']}")
        return 1

    if args.command == "explain":
        print(json.dumps(identity.explain(args.url, args.title, args.company,
                                          args.location, args.text), indent=2))
        return 0

    started = time.monotonic()
    answer = identity.lookup(args.url, args.title, args.company, args.location, args.text)
    took = (time.monotonic() - started) * 1000
    print(f"{answer.verdict.upper()}: {answer.reason}")
    for match in answer.matches:
        print(f"  level {match.level} {match.confidence:8} {match.rule}")
        print(f"    {match.display_id} {match.status or '?'} {match.title} @ {match.company}")
        for item in match.evidence:
            note = f"  ({item.note})" if item.note else ""
            print(f"    {item.compared}: {item.mine!r} vs {item.theirs!r}{note}")
    if answer.stale:
        print(f"  (these sources were behind and have been re-read: {', '.join(answer.stale)})")
    print(f"  {answer.searched} jobs in the index, {took:.0f} ms")
    return {identity.BLOCK: 2, identity.WARN: 1}.get(answer.verdict, 0)


if __name__ == "__main__":
    sys.exit(main())
