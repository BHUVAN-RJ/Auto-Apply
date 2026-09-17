"""Thin OpenRouter chat client.

Deliberately minimal: no framework. `complete` is one request-response, which
is all the tailor needs; `stream` yields text deltas for the interviewer,
whose reply is read as it is written. Keeping this small means the model
provider can be swapped by editing one file.
"""

from __future__ import annotations

import json
import os
from typing import Iterator
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

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
    """Chat completion over a full message history. Returns the assistant's text."""
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
