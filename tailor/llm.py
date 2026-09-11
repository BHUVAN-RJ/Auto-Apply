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
TIMEOUT = 180.0


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
    max_tokens: int = 8000,
) -> str:
    """Single chat completion. Returns the assistant's text."""
    payload = {
        "model": model or tailor_model(),
        "temperature": temperature,
        "max_tokens": max_tokens,
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
    return data["choices"][0]["message"]["content"]
