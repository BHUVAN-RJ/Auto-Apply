"""Claude through the Claude Code subscription already on this machine.

`byhand.py` exists because Opus through the API is the most expensive call in
the app and the person is already paying for Claude: it stops the pipeline,
hands over the prompt, and takes the pasted reply back. This is the same
trade without the paste. `claude -p` is Claude Code's headless mode; it
authenticates with the subscription on this machine, so a tailoring call
costs rate limit rather than an OpenRouter invoice.

**Measured before it was written** (2026-10-30, this machine, the real
`tailor/rules.md` as the system prompt and a 16 KB posting as the user turn):

| call | cache written | cache read | reported cost | wall |
|---|---|---|---|---|
| cold | 18,006 | 10,763 | $0.1487 | 2.7 s |
| warm | 0 | 28,769 | **$0.0083** | 3.1 s |

The prefix is cached for an hour, so a second attempt on the same job and
the next job in the same session read it rather than paying for it. For
comparison, the same prompt on `z-ai/glm-5.3` through OpenRouter measured
$0.0109 cold and $0.0015 warm - and Opus rewrote five of six EXPERIENCE
bullets on the posting where GLM rewrote one (2026-09-24). With no
`ANTHROPIC_API_KEY` set the dollar figure is what the call *would* have cost
on the API; what it actually spends is the subscription's own allowance.

Three things this is careful about:

- **The harness is not free.** A bare `claude -p` loaded 100,268 tokens of
  tools and settings before reading a word of the prompt. `--allowed-tools ""`
  and `--setting-sources ""` are not tidiness: without the second one every
  call would carry this repository's own `CLAUDE.md`, which is the largest
  file in it.
- **It is one more provider behind one door.** `llm.chat` is the only place
  that talks to a model; this is reached from there and nowhere else, so
  every call site keeps working unchanged.
- **It never gets tools.** The model is asked for text. It has no Bash, no
  file access and no network, which is the whole difference between this and
  running an agent.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from typing import Optional

# What `Job.tailor_model` holds for this path. Deliberately not a model name -
# the same decision `byhand.BY_HAND` makes - so nothing can hand it to a
# provider as one.
CLAUDE_CODE = "claude-code"

# The model the subscription is asked for. Overridable, because which model a
# subscription may ask for is a property of the account, not of this code.
MODEL = os.environ.get("CLAUDE_CODE_MODEL", "opus")

BINARY = os.environ.get("CLAUDE_CODE_BIN", "claude")
TIMEOUT = float(os.environ.get("CLAUDE_CODE_TIMEOUT", "600"))

# Flags that make a completion out of an agent. Every one of them is load
# bearing; the measurements in the docstring are with all of them on.
FLAGS = (
    "-p",                      # headless: read the prompt, print the reply
    "--output-format", "json", # so the usage and the errors can be read
    "--allowed-tools", "",     # no Bash, no files, no web: text in, text out
    "--setting-sources", "",   # no CLAUDE.md, no project settings, no plugins
    "--strict-mcp-config",     # no MCP server from the person's own config
)


# The one place in this app where the model is given tools, and the two it
# gets are read-only: look something up on the public web and read the page.
# Everything above still holds for `reply`; this is for the one job that
# cannot be done from the prompt alone - finding out who a company's
# recruiters are - and it exists to keep that off a paid credit.
RESEARCH_TOOLS = "WebSearch WebFetch"
RESEARCH_TIMEOUT = float(os.environ.get("CLAUDE_CODE_RESEARCH_TIMEOUT", "180"))


class NotAvailable(RuntimeError):
    pass


def available() -> bool:
    return shutil.which(BINARY) is not None


def flatten(messages: list[dict]) -> tuple[str, str]:
    """A message list as one system prompt and one user turn.

    `claude -p` takes a prompt, not a conversation, and the tailor's retry
    sends four turns: the rules, the request, the attempt that was rejected
    and why. Flattening them keeps the whole exchange in front of the model -
    marked by role, because an attempt and the rejection of it read as the
    same thing otherwise.

    The system prompt is kept separate rather than folded in, so the stable
    half is a stable prefix and caches.
    """
    system = "\n\n".join(m.get("content") or "" for m in messages
                         if m.get("role") == "system").strip()
    parts: list[str] = []
    rest = [m for m in messages if m.get("role") != "system"]
    for message in rest:
        role, content = message.get("role"), (message.get("content") or "")
        if len(rest) == 1:
            parts.append(content)
        elif role == "assistant":
            parts.append(f"## Your previous reply\n\n{content}")
        else:
            parts.append(f"## Request\n\n{content}" if message is rest[0] else content)
    return system, "\n\n".join(parts).strip()


def _run(system: str, user: str, model: str, timeout: float,
         tools: str = "") -> dict:
    command = [BINARY, *FLAGS, "--model", model]
    if tools:
        # The CLI takes the last value, so this replaces the empty allow-list
        # in FLAGS rather than having to be spliced into it.
        command += ["--allowed-tools", tools]
    if system:
        command += ["--system-prompt", system]
    # The prompt goes in on stdin: a tailoring user turn is 16 KB and an
    # argument list is not the place for it.
    try:
        done = subprocess.run(command, input=user, capture_output=True, text=True,
                              timeout=timeout, check=False,
                              # The CLI reads the key if one is exported and
                              # would then bill the API instead of the
                              # subscription. This path is the subscription.
                              env={k: v for k, v in os.environ.items()
                                   if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")})
    except FileNotFoundError as error:
        raise NotAvailable(f"{BINARY} is not on PATH") from error
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"claude -p did not answer within {timeout:.0f}s") from error
    if done.returncode != 0:
        detail = (done.stderr or done.stdout or "").strip()[:400]
        raise RuntimeError(f"claude -p exited {done.returncode}: {detail}")
    try:
        return json.loads(done.stdout or "{}")
    except ValueError as error:
        raise RuntimeError(f"claude -p did not answer JSON: {(done.stdout or '')[:300]}"
                           ) from error


def reply(messages: list[dict], model: Optional[str] = None,
          timeout: Optional[float] = None) -> tuple[str, dict]:
    """The assistant's text and what the call used.

    Raises rather than returning an empty string: a caller that cannot tell
    "the model said nothing" from "the model was not asked" writes an empty
    resume to disk.
    """
    if not available():
        raise NotAvailable(f"{BINARY} is not on PATH; "
                           "the Claude Code path needs the CLI it is named after")
    system, user = flatten(messages)
    if not user:
        raise ValueError("nothing to send: every message was a system prompt")
    started = time.monotonic()
    data = _run(system, user, model or MODEL, timeout or TIMEOUT)
    if data.get("is_error"):
        raise RuntimeError(f"claude -p: {str(data.get('result') or data)[:300]}")
    text = data.get("result")
    if not isinstance(text, str) or not text.strip():
        raise RuntimeError(f"claude -p returned no text: {str(data)[:300]}")
    usage = dict(data.get("usage") or {})
    usage["seconds"] = round(time.monotonic() - started, 1)
    usage["reported_cost"] = data.get("total_cost_usd")
    usage["model"] = data.get("modelUsage") and list(data["modelUsage"]) or model or MODEL
    return text, usage


def research(prompt: str, model: Optional[str] = None,
             timeout: Optional[float] = None,
             system: str = "") -> tuple[str, dict]:
    """One question answered with the public web, on the subscription.

    Separate from `reply` because it is the one call that gets tools, and
    because what it costs is an allowance rather than a credit: finding out
    who a company's recruiters are was previously a paid lookup, and the
    subscription is already being paid for. Read-only tools only, and the
    caller is expected to treat every word of the answer as a claim about
    the world that it has to check before acting on - an address in
    particular, which is why the outreach path labels one found this way a
    guess and still asks the person to tick it.
    """
    if not available():
        raise NotAvailable(f"{BINARY} is not on PATH")
    if not prompt.strip():
        raise ValueError("nothing to research")
    started = time.monotonic()
    data = _run(system, prompt, model or MODEL,
                timeout or RESEARCH_TIMEOUT, tools=RESEARCH_TOOLS)
    if data.get("is_error"):
        raise RuntimeError(f"claude -p: {str(data.get('result') or data)[:300]}")
    text = data.get("result")
    if not isinstance(text, str) or not text.strip():
        raise RuntimeError(f"claude -p returned no text: {str(data)[:300]}")
    usage = dict(data.get("usage") or {})
    usage["seconds"] = round(time.monotonic() - started, 1)
    usage["reported_cost"] = data.get("total_cost_usd")
    return text, usage
