"""Thin OpenRouter chat client.

Deliberately minimal: no framework. `complete` is one request-response, which
is all the tailor needs; `stream` yields text deltas for the interviewer,
whose reply is read as it is written. Keeping this small means the model
provider can be swapped by editing one file.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Iterator
from pathlib import Path
from typing import Optional

import httpx

from tailor import claudecode

import paths

ROOT = Path(__file__).resolve().parent.parent
paths.load_env()

log = logging.getLogger("llm")

API_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "z-ai/glm-5.3"
TIMEOUT = 300.0

# A tailored resume is the whole LaTeX file, commonly 6-8k tokens, and the
# default models here think before answering — those reasoning tokens come out
# of the same budget. Too low a cap and the reply is truncated before any
# content is written at all, which arrives as a null content field.
DEFAULT_MAX_TOKENS = 32000

# Reasoning tokens come out of the same budget as the answer. Left unbounded,
# a thinking model can spend the entire allowance deliberating and return a
# null content. Tailoring is a careful edit, not a puzzle, so a low effort is
# both sufficient and much cheaper.
REASONING_EFFORT = os.environ.get("OPENROUTER_REASONING_EFFORT", "low")


class LLMError(RuntimeError):
    pass


def tailor_model() -> str:
    return os.environ.get("OPENROUTER_TAILOR_MODEL", DEFAULT_MODEL)


# The model a job is tailored with when the banner's "Use Opus" was pressed.
# Measured on one posting (Abridge, 2026-09-24): the default rewrote one
# experience bullet, this one rewrote five, and the cheap field in between
# either matched the default or hit the same volume by inventing framing.
# It costs about eight times as much per job, which is why it is a button
# and not the default.
DEFAULT_PREMIUM_MODEL = "anthropic/claude-opus-5.5"


def premium_model() -> str:
    """What "Use Opus" puts on the job's row.

    Three answers, in the order they cost the person money. The
    subscription they already pay for comes first when it is switched on and
    the CLI is there (`settings.claude_code`, measured at $0.0083 a warm call
    against $0.21 a job on the API). Then `byhand.BY_HAND`, which is not a
    model at all: the pipeline stops and offers the prompt to copy. Only with
    both off is the API model bought. Resolved here so the banner, the review
    thread and the pipeline cannot disagree about what the word means.
    """
    from server import settings
    from tailor import byhand

    if settings.claude_code() and claudecode.available():
        return claudecode.CLAUDE_CODE
    if settings.opus_by_hand():
        return byhand.BY_HAND
    return os.environ.get("OPENROUTER_PREMIUM_MODEL", DEFAULT_PREMIUM_MODEL)


def outreach_model() -> str:
    """Who writes a follow-up email.

    The emails are the one piece of prose that goes out over the person's own
    name to a named human, and they are three per job rather than one per
    attempt, so the cheap tailoring model is the wrong default here while the
    subscription is switched on and sitting idle (`settings.claude_code`,
    measured at $0.0083 a warm call). `OPENROUTER_OUTREACH_MODEL` overrides,
    for a run with the CLI unavailable or a model being compared; with the
    switch off this is the tailoring model exactly as before.
    """
    named = os.environ.get("OPENROUTER_OUTREACH_MODEL", "").strip()
    if named:
        return named
    from server import settings

    if settings.claude_code() and claudecode.available():
        return claudecode.CLAUDE_CODE
    return tailor_model()


def premium_api_model() -> str:
    return os.environ.get("OPENROUTER_PREMIUM_MODEL", DEFAULT_PREMIUM_MODEL)


def is_sentinel(model: Optional[str]) -> bool:
    """Is this a decision about who answers rather than a model name?

    `byhand.BY_HAND` and `claudecode.CLAUDE_CODE` are both parked on a job's
    row in the slot a model name goes in, and both are deliberately not model
    names. Only the pipeline knows what to do with the first one, so the
    answer has to be askable here, at the one door, rather than trusted to
    every caller: `workshop.workshop_model()` asks `premium_model()` for a
    name it is going to send, got `by-hand`, and OpenRouter answered
    `400 by-hand is not a valid model ID` three times
    (`logs/server.log`, from `workshop.learn` in the background).
    """
    # Imported here, not at the top: `byhand` imports `tailor.tailor`, which
    # imports this module.
    from tailor import byhand

    name = (model or "").strip().lower()
    return name == claudecode.CLAUDE_CODE or byhand.is_by_hand(name)


# Some providers refuse a request with reasoning disabled (Z.AI's GLM
# endpoints answer 400 "Reasoning is mandatory"). A lookup-style call asks
# for the least thinking the model allows rather than none.
NO_REASONING_REFUSED = ("z-ai/",)


def minimal_reasoning(model: str) -> dict:
    if model.startswith(NO_REASONING_REFUSED):
        return {"effort": "low"}
    return {"enabled": False}


def _api_key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not key:
        raise LLMError(
            "OPENROUTER_API_KEY is unset. Copy .env.example to .env and add your key."
        )
    return key


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_api_key()}",
        "Content-Type": "application/json",
        # OpenRouter uses these for attribution on its dashboard.
        "HTTP-Referer": "https://github.com/BHUVAN-RJ/Auto-Apply",
        "X-Title": "Auto-Apply",
    }


def _payload(messages: list[dict], model: Optional[str], temperature: float,
             max_tokens: int, reasoning: Optional[dict]) -> dict:
    return {
        "model": model or tailor_model(),
        "temperature": temperature,
        "max_tokens": max_tokens,
        "reasoning": reasoning or {"effort": REASONING_EFFORT},
        "messages": messages,
    }


def complete(
    system: str,
    user: str,
    model: Optional[str] = None,
    temperature: float = 0.3,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    reasoning: Optional[dict] = None,
) -> str:
    """Single chat completion. Returns the assistant's text.

    `reasoning` overrides the default low-effort thinking; pass
    {"enabled": False} for a call that is a lookup rather than an edit.
    """
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    return chat(messages, model=model, temperature=temperature,
                max_tokens=max_tokens, reasoning=reasoning)


def chat(
    messages: list[dict],
    model: Optional[str] = None,
    temperature: float = 0.3,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    reasoning: Optional[dict] = None,
) -> str:
    """Chat completion over a full message history. Returns the assistant's text.

    One door, two providers. `claudecode.CLAUDE_CODE` is answered by the
    Claude Code subscription on this machine instead of by OpenRouter; every
    call site - the tailor, the cover letter, the screen, the answers, the
    story pick - reaches it through here and so needs no knowledge of which
    one ran.
    """
    wanted = (model or tailor_model()).strip()
    if is_sentinel(wanted):
        # Routed or refused, never forwarded. A sentinel in the payload is a
        # 400 from the provider and, worse, a call site quietly believing a
        # model answered it.
        return _by_sentinel(wanted, messages)
    payload = _payload(messages, model, temperature, max_tokens, reasoning)
    try:
        response = httpx.post(API_URL, json=payload, headers=_headers(), timeout=TIMEOUT)
    except httpx.HTTPError as exc:
        raise LLMError(f"OpenRouter request failed: {exc}") from exc

    if response.status_code != 200:
        raise LLMError(f"OpenRouter returned {response.status_code}: {response.text[:400]}")

    data = response.json()
    if "choices" not in data or not data["choices"]:
        raise LLMError(f"OpenRouter returned no choices: {str(data)[:400]}")

    _log_usage(data, model or tailor_model())
    choice = data["choices"][0]
    content = (choice.get("message") or {}).get("content")
    if not content:
        # A reasoning model that spends its whole budget thinking returns a
        # null content with finish_reason "length". Say so plainly rather than
        # letting None travel on into the parser.
        reason = choice.get("finish_reason")
        usage = data.get("usage") or {}
        detail = f"finish_reason={reason}, max_tokens={max_tokens}"
        if reason == "length":
            detail += (
                f", completion_tokens={usage.get('completion_tokens')} "
                "(the reply was cut off before any content was written; "
                "raise max_tokens or use a model that reasons less)"
            )
        refusal = (choice.get("message") or {}).get("refusal")
        if refusal:
            detail += f", refusal={refusal!r}"
        raise LLMError(f"{model or tailor_model()} returned no content: {detail}")
    return content


# Prompt caching, asked about 2026-10-03: nothing here asks for it. The
# question is first whether it is already happening - OpenRouter passes a
# provider's automatic cache straight through, and only Anthropic and
# Gemini need explicit `cache_control` breakpoints - so the counts are
# logged rather than guessed at. A tailoring run sends the rules, the master
# resume, the posting and the stories (11k tokens measured) up to four times
# for one job and again for the next, so the repeated prefix is most of
# every call; `cached_tokens` on a second attempt is what says whether that
# prefix is being charged twice.
def _by_sentinel(wanted: str, messages: list[dict]) -> str:
    """Answer a sentinel, or say plainly that it cannot be answered here.

    `claude-code` has an answer on this machine: the subscription. `by-hand`
    means *the person is the model*, and only the pipeline can act on that
    (`pipeline.process` stops and writes `handoff.md`); a caller that reached
    this far wanted text back from a provider, so the honest reply is a
    refusal naming what happened. The subscription is offered first, because
    a caller asking for the strongest model without an API bill is asking for
    exactly that and the CLI is the same trade without the paste.
    """
    if wanted.lower() == claudecode.CLAUDE_CODE:
        return _by_subscription(messages)
    if claudecode.available():
        log.warning("%s reached llm.chat as a model name; answered by the "
                    "Claude Code subscription instead of a provider", wanted)
        return _by_subscription(messages)
    raise LLMError(
        f"{wanted!r} is not a model name: it is the decision to hand the "
        "prompt to the person instead of to a provider, and only the "
        "pipeline can act on it (byhand.is_by_hand). Pass a real model, or "
        "llm.premium_api_model() for the paid one."
    )


def _by_subscription(messages: list[dict]) -> str:
    """The subscription path. Errors come back as `LLMError`, like every
    other provider failure, so a pipeline that already handles one handles
    this one."""
    try:
        text, usage = claudecode.reply(messages)
    except claudecode.NotAvailable as error:
        raise LLMError(f"the Claude Code path is on but unusable: {error}") from error
    except (RuntimeError, ValueError) as error:
        raise LLMError(f"claude -p failed: {error}") from error
    # `claude -p` reports `input_tokens` as the uncached remainder only - it
    # was 2 on a 29,000-token prompt - so the total is the three counts
    # added, or the logged cache percentage reads as 500,000%.
    read = usage.get("cache_read_input_tokens") or 0
    written = usage.get("cache_creation_input_tokens") or 0
    _log_usage({"usage": {
        "prompt_tokens": (usage.get("input_tokens") or 0) + read + written,
        "completion_tokens": usage.get("output_tokens"),
        "cost": usage.get("reported_cost"),
        "prompt_tokens_details": {"cached_tokens": read},
    }}, f"{claudecode.CLAUDE_CODE} ({usage.get('seconds')}s)")
    return text


def _log_usage(data: dict, model: str) -> None:
    usage = data.get("usage") or {}
    if not usage:
        return
    details = usage.get("prompt_tokens_details") or {}
    cached = details.get("cached_tokens") or usage.get("cache_read_input_tokens") or 0
    prompt = usage.get("prompt_tokens") or 0
    log.info("%s: prompt=%s cached=%s (%s%%) completion=%s cost=%s", model, prompt, cached,
             round(100 * cached / prompt) if prompt else 0,
             usage.get("completion_tokens"), usage.get("cost"))


def stream(
    messages: list[dict],
    model: Optional[str] = None,
    temperature: float = 0.3,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    reasoning: Optional[dict] = None,
) -> Iterator[str]:
    """Chat completion as a stream of text deltas.

    Reasoning deltas are skipped; only content is yielded. An error status
    is raised before the first delta, so a caller that has started
    forwarding text never sees one mid-reply.
    """
    # The same door rule as `chat`, refused rather than routed: neither
    # sentinel streams. `claude -p` prints one reply, and `by-hand` is a
    # person. The interviewer is the only caller and never passes one; this
    # is here so that a sentinel can reach a provider from nowhere at all.
    wanted = (model or tailor_model()).strip()
    if is_sentinel(wanted):
        raise LLMError(f"{wanted!r} is not a model name and cannot stream; "
                       "pass a real model.")
    payload = _payload(messages, model, temperature, max_tokens, reasoning) | {"stream": True}
    try:
        with httpx.stream("POST", API_URL, json=payload, headers=_headers(), timeout=TIMEOUT) as response:
            if response.status_code != 200:
                body = response.read().decode(errors="replace")
                raise LLMError(f"OpenRouter returned {response.status_code}: {body[:400]}")
            for line in response.iter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[6:].strip()
                if data == "[DONE]":
                    break
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if event.get("error"):
                    raise LLMError(f"OpenRouter stream error: {str(event['error'])[:400]}")
                for choice in event.get("choices") or []:
                    text = (choice.get("delta") or {}).get("content")
                    if text:
                        yield text
    except httpx.HTTPError as exc:
        raise LLMError(f"OpenRouter request failed: {exc}") from exc
