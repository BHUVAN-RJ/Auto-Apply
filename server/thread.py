"""One conversation per job: ask about it, or ask for a change.

The review page's "Ask for an answer" card answered one question and
forgot it. This keeps the turns, so a screener's questions and the
re-tailors that follow read as one thread about one application, with the
posting, the tailored resume, the cover letter, the picked stories and the
applicant facts behind every reply.

The thread lives under `data/threads/<job_id>.json`, not in the
application folder: a re-tailor writes a **new** folder each time, and the
conversation is about the job, not about one run of it.

Two kinds of turn:

- `ask` is `tailor/answers.py`, the same context the fill's
  `answer_question` action uses, and the answer is still appended to that
  run's `answers.md`. A visa / work-authorisation question is refused
  here as everywhere else.
- `change` is `pipeline.retailor(..., keep_status=True)`: new documents in
  a new folder, the queue row pointed at them, and the job's standing left
  alone. A filled or submitted job does not walk back to checkpoint 1 and
  no fill is started; the new resume is there to be handed over by hand.

A change runs in a thread of its own (minutes, two model calls and a
LaTeX compile), so the turn is written as `running` and the page polls
until it is `done` or `error`. One change at a time per job.
"""

from __future__ import annotations

import json
import os
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import paths

ROOT = Path(__file__).resolve().parent.parent
THREADS = Path(os.environ.get("AUTOPILOT_THREADS", paths.DATA / "threads"))

# How much of the conversation is put in front of the model with a new
# question. Enough for "make that shorter" to mean something, not so much
# that a long thread pushes the posting out of the context.
HISTORY_TURNS = 8
HISTORY_CHARS = 4000

_lock = threading.Lock()
_running: dict[str, str] = {}   # job id -> the turn id being tailored


class ThreadError(RuntimeError):
    pass


def path_for(job_id: str) -> Path:
    return THREADS / f"{job_id}.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def load(job_id: str) -> list[dict]:
    path = path_for(job_id)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return []
    return data.get("turns", []) if isinstance(data, dict) else []


def _save(job_id: str, turns: list[dict]) -> None:
    _write_atomic(path_for(job_id), json.dumps({"job": job_id, "turns": turns}, indent=2) + "\n")


def _update(job_id: str, fn) -> list[dict]:
    """Read-modify-write under one lock: a change's worker thread and the
    page's next message both write the same file."""
    with _lock:
        turns = load(job_id)
        fn(turns)
        _save(job_id, turns)
        return turns


def _turn(turns: list[dict], turn_id: str) -> Optional[dict]:
    for turn in turns:
        if turn.get("id") == turn_id:
            return turn
    return None


def add(job_id: str, kind: str, text: str, **fields) -> dict:
    """One turn on the end of the thread. `kind` is what it is, not who
    said it: `question`, `answer`, `change`, `result`, `error`."""
    turn = {"id": f"{len(load(job_id)) + 1}-{_now()}", "kind": kind, "text": text,
            "when": _now(), **fields}
    _update(job_id, lambda turns: turns.append(turn))
    return turn


def set_fields(job_id: str, turn_id: str, **fields) -> None:
    def apply(turns: list[dict]) -> None:
        turn = _turn(turns, turn_id)
        if turn is not None:
            turn.update(fields)
    _update(job_id, apply)


def running(job_id: str) -> bool:
    return job_id in _running


def history(job_id: str) -> str:
    """The recent turns as plain text, for the model's context. Trimmed
    from the end: the last thing said matters most."""
    lines: list[str] = []
    for turn in reversed(load(job_id)[-HISTORY_TURNS:]):
        who = "You" if turn["kind"] in ("question", "change") else "Autopilot"
        body = " ".join((turn.get("text") or "").split())
        if not body:
            continue
        line = f"{who}: {body}"
        if sum(len(x) for x in lines) + len(line) > HISTORY_CHARS:
            break
        lines.append(line)
    return "\n".join(reversed(lines))


def start_change(job_id: str, instruction: str, run) -> dict:
    """The `change` turn plus a `result` turn kept at `running` while the
    re-tailor works. `run()` is called off the request thread and returns
    the new folder's name."""
    if running(job_id):
        raise ThreadError("a re-tailor is already running for this job")
    add(job_id, "change", instruction)
    result = add(job_id, "result", "", state="running")
    _running[job_id] = result["id"]

    def work() -> None:
        try:
            folder = run()
            set_fields(job_id, result["id"], state="done", folder=str(folder),
                       text="Re-tailored. The resume and the letter above are the new ones.")
            # A request the person keeps making on every job belongs in the
            # prompts; the Workshop proposes it once enough have piled up.
            from tailor import workshop
            workshop.maybe_learn()
        except Exception as exc:  # noqa: BLE001 - the turn carries the failure
            traceback.print_exc()
            set_fields(job_id, result["id"], state="error", kind="error",
                       text=f"{type(exc).__name__}: {exc}")
        finally:
            _running.pop(job_id, None)

    threading.Thread(target=work, name=f"retailor-{job_id}", daemon=True).start()
    return result
