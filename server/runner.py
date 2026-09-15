"""Launching pipeline work from the review page.

Runs the work as a detached subprocess rather than inside the web server. A
browser-driving agent takes minutes, opens a real Chrome window, and can crash;
none of that should block an HTTP request or take the server down with it.

Output goes to a log file inside the application folder, so a run that fails
after the page has moved on is still diagnosable.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
# The server is started from a shell that has not sourced .env; the scripts
# it launches load it themselves, but this module's own switch lives there.
load_dotenv(ROOT / ".env")

# Approval marks the job; it does not launch the browser. Filling is started
# explicitly, from the review page's button or apply.py, so that a run can be
# repeated as often as needed and a half-finished one is never ambiguous.
# Set AUTOPILOT_AUTOFILL=1 to have approval start the fill immediately.
AUTOFILL_ENABLED = os.environ.get("AUTOPILOT_AUTOFILL", "") not in ("", "0", "false")


def python_executable() -> str:
    """The interpreter running the server, so the venv is inherited."""
    return sys.executable or "python3"


# One fill per job. Two apply.py runs on the same job drive the same Chrome
# tab and undo each other's steps, which is exactly what happened when approve
# started one and "Fill it again" started a second three seconds later.
_fills: dict[str, tuple[subprocess.Popen, float]] = {}


def launch(script: str, job_id: str, log_dir: Optional[Path] = None) -> Optional[int]:
    """Start `script <job_id>` detached. Returns its pid, or None if not started."""
    target = ROOT / script
    if not target.exists():
        return None
    if script == "apply.py" and fill_pid(job_id):
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
    if script == "apply.py":
        _fills[job_id] = (process, time.time())
    return process.pid


def _pgrep_fill(job_id: str) -> Optional[int]:
    """A fill this server did not start (an earlier server, or the shell)."""
    try:
        out = subprocess.run(
            ["pgrep", "-f", f"apply.py {job_id}$"], capture_output=True, text=True,
            timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    pids = [int(p) for p in out.split() if p.isdigit() and int(p) != os.getpid()]
    return pids[0] if pids else None


def fill_pid(job_id: str) -> Optional[int]:
    """The pid of the fill running for this job, or None."""
    entry = _fills.get(job_id)
    if entry is not None:
        process, _ = entry
        if process.poll() is None:
            return process.pid
        del _fills[job_id]
    return _pgrep_fill(job_id)


def fill_started_at(job_id: str) -> Optional[float]:
    """When the running fill began, if this server started it."""
    entry = _fills.get(job_id)
    if entry is None or entry[0].poll() is not None:
        return None
    return entry[1]


def stop_fill(job_id: str) -> Optional[int]:
    """Kill the fill running for this job. Returns the pid it killed, or None.

    The whole process group goes, because apply.py spawns browser-use workers
    of its own. Chrome is not in that group (browser/chrome.py detaches it),
    so the window and the half-filled form survive.
    """
    pid = fill_pid(job_id)
    if pid is None:
        return None
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except PermissionError:
        os.kill(pid, signal.SIGTERM)
    entry = _fills.pop(job_id, None)
    if entry is not None:
        try:
            entry[0].wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(pid, signal.SIGKILL)
    else:
        for _ in range(50):
            if _pgrep_fill(job_id) is None:
                break
            time.sleep(0.1)
    return pid


def start_fill(job_id: str, log_dir: Optional[Path] = None) -> Optional[int]:
    """Begin filling an approved application, if autofill is switched on."""
    if not AUTOFILL_ENABLED:
        return None
    return launch("apply.py", job_id, log_dir)


def start_pipeline(job_id: str, log_dir: Optional[Path] = None) -> Optional[int]:
    """Tailor and compile one queued job."""
    return launch("pipeline.py", job_id, log_dir)
