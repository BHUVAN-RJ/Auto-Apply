"""Launching pipeline work from the review page.

Runs the work as a detached subprocess rather than inside the web server. A
browser-driving agent takes minutes, opens a real Chrome window, and can crash;
none of that should block an HTTP request or take the server down with it.

Output goes to a log file inside the application folder, so a run that fails
after the page has moved on is still diagnosable.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent

# Set AUTOPILOT_NO_AUTOFILL=1 to keep approval and form filling separate.
AUTOFILL_DISABLED = os.environ.get("AUTOPILOT_NO_AUTOFILL", "") not in ("", "0", "false")


def python_executable() -> str:
    """The interpreter running the server, so the venv is inherited."""
    return sys.executable or "python3"


def launch(script: str, job_id: str, log_dir: Optional[Path] = None) -> Optional[int]:
    """Start `script <job_id>` detached. Returns its pid, or None if not started."""
    target = ROOT / script
    if not target.exists():
        return None

    log_path = (log_dir or ROOT / "data") / f"{script.replace('.py', '')}_{job_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = open(log_path, "a")
    log.write(f"\n--- {script} {job_id} ---\n")
    log.flush()

    process = subprocess.Popen(
        [python_executable(), str(target), job_id],
        cwd=str(ROOT),
        stdout=log,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        # Its own process group, so stopping the server does not kill a fill
        # halfway through and leave a half-completed form behind.
        start_new_session=True,
    )
    return process.pid


def start_fill(job_id: str, log_dir: Optional[Path] = None) -> Optional[int]:
    """Begin filling an approved application, unless autofill is switched off."""
    if AUTOFILL_DISABLED:
        return None
    return launch("apply.py", job_id, log_dir)


def start_pipeline(job_id: str, log_dir: Optional[Path] = None) -> Optional[int]:
    """Tailor and compile one queued job."""
    return launch("pipeline.py", job_id, log_dir)
