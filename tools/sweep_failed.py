"""Move failed application folders under `applications/failed/`.

Every run allocates its own folder, so a job that failed four times before
succeeding leaves four dead folders next to the live one. This moves each
folder whose `status.json` reads `failed` into `applications/failed/`, and
repoints the queue row if it still referred to the moved folder, so the
review page keeps finding the error and the rejected attempts.

Nothing is deleted. A folder that was moved can be moved back by hand.

    python tools/sweep_failed.py            # move them
    python tools/sweep_failed.py --dry-run  # only list them
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from archive import store  # noqa: E402
from server import queue  # noqa: E402

FAILED_DIR_NAME = "failed"


def failed_folders(applications: Path) -> list[Path]:
    out = []
    for path in sorted(applications.iterdir()):
        if not path.is_dir() or path.name == FAILED_DIR_NAME:
            continue
        status = path / "status.json"
        if not status.exists():
            continue
        try:
            if json.loads(status.read_text()).get("status") == "failed":
                out.append(path)
        except json.JSONDecodeError:
            continue
    return out


def sweep(applications: Path | None = None, dry_run: bool = False) -> list[tuple[Path, Path]]:
    applications = applications or store.APPLICATIONS
    target_dir = applications / FAILED_DIR_NAME
    moved = []
    for path in failed_folders(applications):
        target = target_dir / path.name
        if target.exists():
            print(f"skip {path.name}: already under {FAILED_DIR_NAME}/", file=sys.stderr)
            continue
        moved.append((path, target))
        if dry_run:
            continue
        target_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(target))
        for job in queue.all_jobs():
            if job.app_dir and Path(job.app_dir) == path:
                queue.update(job.id, app_dir=str(target))
    return moved


def main(argv: list[str]) -> int:
    dry_run = "--dry-run" in argv
    moved = sweep(dry_run=dry_run)
    verb = "would move" if dry_run else "moved"
    for source, target in moved:
        print(f"{verb} {source.name} -> {target.parent.name}/")
    print(f"{len(moved)} folder(s) {verb}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
