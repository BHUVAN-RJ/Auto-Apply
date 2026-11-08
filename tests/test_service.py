"""The launcher must never start a second server beside a supervised one.

`scripts/service.sh` installs a launchd agent that owns the server, so that
copy has no pid file of the launcher's. Before this, the launcher read a
missing pid file as "no server", picked the next free port and started
another one - and two servers mean two data folders and a queue that reads
empty, which is the first entry in CLAUDE.md's list of what bites you.

These run the real script against a stand-in that answers the two endpoints
the launcher identifies its own server by.
"""

from __future__ import annotations

import http.server
import json
import socket
import subprocess
import threading
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "autopilot"


class _Handler(http.server.BaseHTTPRequestHandler):
    """Answers /health and /setup the way the server does, nothing else."""

    def do_GET(self):  # noqa: N802 - the stdlib's name
        if self.path in ("/health", "/setup"):
            body = json.dumps({"ok": True, "home": "/tmp/stand-in"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def log_message(self, *_args):
        pass


@pytest.fixture
def standin():
    """An HTTP server on a free port, answering as ours would."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = http.server.HTTPServer(("127.0.0.1", port), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        server.shutdown()
        server.server_close()


def _run(argument: str, home: Path, port: int) -> subprocess.CompletedProcess:
    run = home / "run"
    run.mkdir(parents=True, exist_ok=True)
    (run / "port").write_text(str(port))
    return subprocess.run(
        [str(SCRIPT), argument],
        capture_output=True,
        text=True,
        timeout=60,
        env={"HOME": str(home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
             "AUTOPILOT_HOME": str(home)},
    )


def test_serve_leaves_a_server_that_already_holds_the_port(standin, tmp_path):
    """`serve` is what the agent runs. It must not fight a live server, and
    it must exit 0 so that KeepAlive's SuccessfulExit=false keeps it down."""
    done = _run("serve", tmp_path, standin)
    assert done.returncode == 0, done.stderr
    assert "already serving" in done.stdout + done.stderr


def test_status_reports_a_server_it_has_no_pid_file_for(standin, tmp_path):
    done = _run("status", tmp_path, standin)
    assert "server  running" in done.stdout, done.stdout + done.stderr
    assert "supervised" in done.stdout


def test_status_says_stopped_when_nothing_answers(tmp_path):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        dead = probe.getsockname()[1]
    done = _run("status", tmp_path, dead)
    assert "server  stopped" in done.stdout, done.stdout + done.stderr
