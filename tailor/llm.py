"""Thin OpenRouter chat client.

Deliberately minimal: one function, no streaming, no framework. The tailor
needs a single request-response per job, and keeping this small means the
model provider can be swapped by editing one file.
"""

from __future__ import annotations

import os
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


def _api_key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not key:
        raise LLMError(
            "OPENROUTER_API_KEY is unset. Copy .env.example to .env and add your key."
        )
    return key


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
    payload = {
        "model": model or tailor_model(),
        "temperature": temperature,
        "max_tokens": max_tokens,
        "reasoning": reasoning or {"effort": REASONING_EFFORT},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    headers = {
        "Authorization": f"Bearer {_api_key()}",
        "Content-Type": "application/json",
        # OpenRouter uses these for attribution on its dashboard.
        "HTTP-Referer": "https://github.com/BHUVAN-RJ/Auto-Apply",
        "X-Title": "Auto-Apply",
    }

    try:
        response = httpx.post(API_URL, json=payload, headers=headers, timeout=TIMEOUT)
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
