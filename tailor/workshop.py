"""The Workshop: the person asks for a change in words, a model edits the
prompts, the person applies it or not.

Two ways in:

- `propose(request)`: typed (or spoken) on the Workshop tab. A cheap call
  picks the prompts the request is about, the stronger model edits them.
- `learn()`: the job threads' re-tailor requests, read across jobs. What
  the person asked for on more than one job, or as a standing rule, comes
  back as a proposal of the same kind, marked `learned`. `maybe_learn()`
  runs it by itself once `LEARN_EVERY` new requests have piled up.

A proposal is a list of search/replace edits, never a whole new prompt: a
model rewriting the 17k-character tailoring rules to add one line drops
things. The edits are applied to the prompts as they are at apply time; if
one no longer matches, nothing is applied and the proposal says so.

Nothing is applied without the person's click. Every applied edit goes
through `prompts.save`, so it is in that prompt's history and one Reset
away from the stock text.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import paths

from . import llm, prompts

MODEL_ENV = "OPENROUTER_WORKSHOP_MODEL"
# New re-tailor requests before learning runs by itself.
LEARN_EVERY = 3
# How many earlier requests are sent along with the new ones, so a
# preference said once last week and once today is seen as two.
LEARN_CONTEXT = 20
MAX_PICK = 3
STORE: Optional[Path] = None

# The fixed instruction the review page sends for "Re-tailor with Opus" on
# an empty box. It asks for a model, not for a change, so it teaches nothing.
OPUS_ONLY = "Re-tailor this on the stronger model."

PICK_PROMPT = """You route a request to the system prompts it is about. Below is the
list of prompts of a job-application assistant, one per line: name, title,
what it does. Reply with the names of the prompts that would have to change
to satisfy the request, most relevant first, at most {n}, one per line,
nothing else. If no prompt could satisfy it, reply with the single word
none."""

EDIT = re.compile(
    r"^<<<<<<< SEARCH[ \t]+([\w.]+)[ \t]*\n(.*?)^=======[ \t]*\n(.*?)^>>>>>>> REPLACE[ \t]*$",
    re.S | re.M,
)

_lock = threading.Lock()
_learning = threading.Lock()


class WorkshopError(RuntimeError):
    pass


def workshop_model() -> str:
    # Editing the prompts every later job runs on is rare and worth the
    # stronger model; the pick in front of it is the cheap one.
    return os.environ.get(MODEL_ENV, "").strip() or llm.premium_model()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# -------------------------------------------------------------- store --


def store_path() -> Path:
    return STORE or paths.DATA / "workshop.json"


def _load() -> dict:
    try:
        data = json.loads(store_path().read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    data.setdefault("proposals", [])
    data.setdefault("learned", [])
    return data


def _save(data: dict) -> None:
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, path)


def proposals() -> list[dict]:
    return _load()["proposals"]


def _find(data: dict, pid: str) -> dict:
    for proposal in data["proposals"]:
        if proposal["id"] == pid:
            return proposal
    raise WorkshopError(f"no proposal {pid!r}")


# -------------------------------------------------------------- edits --


def parse(reply: str) -> tuple[str, list[dict]]:
    """(message to the person, edits). The message is whatever comes
    before the first edit block."""
    first = reply.find("<<<<<<< SEARCH")
    message = (reply if first < 0 else reply[:first]).strip()
    edits = [{"prompt": m.group(1), "search": m.group(2), "replace": m.group(3)}
             for m in EDIT.finditer(reply)]
    return message, edits


def apply_edits(edits: list[dict]) -> tuple[dict[str, str], list[str]]:
    """({prompt: new text}, problems). Applied in order, against the
    prompts as they are now; an edit that does not match is a problem."""
    texts: dict[str, str] = {}
    problems: list[str] = []
    for edit in edits:
        name = edit["prompt"]
        if name not in prompts.BY_NAME:
            problems.append(f"{name}: there is no prompt by that name")
            continue
        current = texts.get(name, prompts.text(name))
        search, replace = edit["search"], edit["replace"]
        if not search.strip():
            texts[name] = current.rstrip("\n") + "\n\n" + replace.strip("\n") + "\n"
            continue
        count = current.count(search)
        if count != 1:
            where = "does not occur" if count == 0 else f"occurs {count} times"
            problems.append(f"{name}: the SEARCH text {where} in the prompt: "
                            f"{search.strip()[:120]!r}")
            continue
        texts[name] = current.replace(search, replace, 1)
    return texts, problems


def diff(name: str, new: str) -> str:
    old = prompts.text(name)
    return "".join(difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile=f"{name} (now)", tofile=f"{name} (proposed)", n=2))


# ------------------------------------------------------------ propose --


def catalog() -> str:
    return "\n".join(f"{p.name} | {p.title} | {p.note}".rstrip(" |")
                     for p in prompts.CATALOG)


def pick(request: str) -> list[str]:
    reply = llm.complete(
        prompts.text("workshop.pick").replace("{n}", str(MAX_PICK)),
        f"## Prompts\n\n{catalog()}\n\n## Request\n\n{request}",
        model=llm.tailor_model(), temperature=0.0, max_tokens=300,
        reasoning=llm.minimal_reasoning(llm.tailor_model()),
    )
    names = []
    for line in (reply or "").splitlines():
        name = line.strip().strip("`*- ").split()[0] if line.strip() else ""
        if name in prompts.BY_NAME and name not in names:
            names.append(name)
    return names[:MAX_PICK]


def _user_message(request: str, names: list[str]) -> str:
    shown = "\n\n".join(
        f"===== prompt: {name} =====\n{prompts.text(name)}\n===== end of {name} ====="
        for name in names
    ) or "(none picked: say what would have to change instead)"
    return (f"## Request\n\n{request}\n\n## Every prompt there is\n\n{catalog()}\n\n"
            f"## The prompts to edit, as they are now\n\n{shown}")


def _system(source: str) -> str:
    rules = prompts.text("workshop")
    if source == "learned":
        return prompts.text("learn") + "\n\n" + rules
    return rules


def propose(request: str, source: str = "you", evidence: Optional[list[dict]] = None) -> dict:
    """Ask the model, record the proposal (pending, or failed), return it."""
    request = request.strip()
    if not request:
        raise WorkshopError("say what should change")
    names = pick(request)
    messages = [{"role": "system", "content": _system(source)},
                {"role": "user", "content": _user_message(request, names)}]
    model = workshop_model()
    reply = llm.chat(messages, model=model, temperature=0.2, max_tokens=12000,
                     reasoning={"effort": "low"})
    message, edits = parse(reply or "")
    texts, problems = apply_edits(edits)
    if problems:
        # One more try, told exactly which blocks missed.
        messages += [{"role": "assistant", "content": reply},
                     {"role": "user", "content": "These edits could not be applied:\n- "
                      + "\n- ".join(problems)
                      + "\nReply again in full: the sentence, then every edit block, "
                        "with SEARCH copied exactly from the prompt text above."}]
        reply = llm.chat(messages, model=model, temperature=0.1, max_tokens=12000,
                         reasoning={"effort": "low"})
        message, edits = parse(reply or "")
        texts, problems = apply_edits(edits)

    proposal = {
        "id": f"w{int(datetime.now().timestamp() * 1000)}",
        "when": _now(),
        "source": source,
        "request": request,
        "message": message or ("No change." if not edits else ""),
        "edits": edits,
        "prompts": sorted(texts),
        "diffs": {name: diff(name, new) for name, new in texts.items()},
        "state": "failed" if problems else ("pending" if texts else "empty"),
        "error": "; ".join(problems),
        "evidence": evidence or [],
    }
    with _lock:
        data = _load()
        data["proposals"].append(proposal)
        _save(data)
    return proposal


def apply(pid: str) -> dict:
    with _lock:
        data = _load()
        proposal = _find(data, pid)
        if proposal["state"] != "pending":
            raise WorkshopError(f"this proposal is {proposal['state']}")
        texts, problems = apply_edits(proposal["edits"])
        if problems:
            proposal["state"], proposal["error"] = "stale", (
                "The prompt changed since this was proposed. Ask again.")
        else:
            why = "learned" if proposal["source"] == "learned" else "workshop"
            for name, new in texts.items():
                prompts.save(name, new, why=why)
            proposal["state"], proposal["applied"] = "applied", _now()
        _save(data)
        return proposal


def dismiss(pid: str) -> dict:
    with _lock:
        data = _load()
        proposal = _find(data, pid)
        if proposal["state"] == "pending":
            proposal["state"] = "dismissed"
        _save(data)
        return proposal


# -------------------------------------------------------------- learn --


def _threads_dir() -> Path:
    from server import thread
    return thread.THREADS


def change_requests() -> list[dict]:
    """Every re-tailor request the person typed, oldest first:
    {key, job, text, when}."""
    found = []
    folder = _threads_dir()
    for path in sorted(folder.glob("*.json")) if folder.exists() else []:
        try:
            turns = json.loads(path.read_text()).get("turns", [])
        except (json.JSONDecodeError, AttributeError):
            continue
        for turn in turns:
            text = " ".join((turn.get("text") or "").split())
            if turn.get("kind") != "change" or not text or text.startswith(OPUS_ONLY):
                continue
            found.append({"key": f"{path.stem}:{turn.get('id')}", "job": path.stem,
                          "text": text, "when": turn.get("when", "")})
    return sorted(found, key=lambda r: r["when"])


def unseen() -> list[dict]:
    learned = set(_load()["learned"])
    return [r for r in change_requests() if r["key"] not in learned]


def _job_label(job_id: str) -> str:
    try:
        from server import queue
        job = queue.get(job_id)
    except Exception:  # noqa: BLE001 - a label is a nicety
        job = None
    if job is None:
        return f"job {job_id}"
    return " @ ".join(x for x in (job.title, job.company) if x) or f"job {job_id}"


def learn() -> Optional[dict]:
    """A `learned` proposal from the requests not learned from yet, or None
    when there are none. The requests are marked learned either way."""
    if not _learning.acquire(blocking=False):
        return None
    try:
        new = unseen()
        if not new:
            return None
        keys = {r["key"] for r in new}
        earlier = [r for r in change_requests() if r["key"] not in keys][-LEARN_CONTEXT:]

        def lines(rows: list[dict]) -> str:
            return "\n".join(f"- [{_job_label(r['job'])}] {r['text']}" for r in rows) or "(none)"

        request = (f"## New change requests\n\n{lines(new)}\n\n"
                   f"## Earlier change requests, already learned from\n\n{lines(earlier)}")
        proposal = propose(request, source="learned",
                           evidence=[{"job": r["job"], "text": r["text"]} for r in new])
        with _lock:
            data = _load()
            data["learned"] = sorted(set(data["learned"]) | keys)
            _save(data)
        return proposal
    finally:
        _learning.release()


def maybe_learn() -> None:
    """Learn in the background once enough new requests have piled up.
    Called after every re-tailor; never raises."""
    try:
        from server import settings
        if not settings.load().get("learn_prompts", True) or len(unseen()) < LEARN_EVERY:
            return
    except Exception:  # noqa: BLE001
        return

    def work() -> None:
        try:
            learn()
        except Exception:  # noqa: BLE001 - a failed learn waits for the next one
            traceback.print_exc()

    threading.Thread(target=work, name="workshop-learn", daemon=True).start()
