"""Fish Audio text to speech over HTTP: one request, one WAV.

Chosen because Kokoro on a laptop CPU takes a second or two per sentence
and the turn stacks that on top of the model and the transcription. Fish
is the cheapest of the natural-sounding hosted voices, and its
`s2.1-pro-free` model costs nothing under fair use (through November
2026, best effort, no SLA). Everything is keyed off AUTOPILOT_FISH_API_KEY:
absent, this backend does not exist and the local ones take over.
"""

from __future__ import annotations

import os

import httpx

URL = "https://api.fish.audio/v1/tts"
DEFAULT_MODEL = "s2.1-pro-free"
# Without a reference the API picks a voice per request, so a streamed
# reply changed speaker every sentence. "EnglishSarah" from the public
# library; any library id overrides it through AUTOPILOT_FISH_VOICE.
DEFAULT_VOICE = "933563129e564b19a115bedd57b7406a"
# Sarah reads flat at the defaults. An emotion tag in the text, a warmer
# sampling temperature and a slightly faster pace were picked by ear against
# the alternatives; all three are overridable from .env.
DEFAULT_EMOTION = "cheerful"
DEFAULT_TEMPERATURE = 0.9
DEFAULT_SPEED = 1.08
# Kokoro's rate, so the page's player and analyser see the same shape.
SAMPLE_RATE = 24000
TIMEOUT = 30.0


class FishError(RuntimeError):
    pass


def api_key() -> str:
    return os.environ.get("AUTOPILOT_FISH_API_KEY", "").strip()


def model() -> str:
    return os.environ.get("AUTOPILOT_FISH_MODEL", "").strip() or DEFAULT_MODEL


# A voice id from fish.audio's library.
def reference_id() -> str:
    return os.environ.get("AUTOPILOT_FISH_VOICE", "").strip() or DEFAULT_VOICE


# "(cheerful)" style tag prefixed to every sentence; blank disables it.
def emotion() -> str:
    return os.environ.get("AUTOPILOT_FISH_EMOTION", DEFAULT_EMOTION).strip()


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def configured() -> bool:
    return bool(api_key())


def speak(text: str) -> bytes:
    tag = emotion()
    body: dict = {
        "text": f"({tag}) {text}" if tag else text,
        "format": "wav",
        "sample_rate": SAMPLE_RATE,
        # Cheaper first byte; the page speaks one sentence at a time anyway.
        "latency": "balanced",
        "reference_id": reference_id(),
        "temperature": _float("AUTOPILOT_FISH_TEMPERATURE", DEFAULT_TEMPERATURE),
        "prosody": {"speed": _float("AUTOPILOT_FISH_SPEED", DEFAULT_SPEED)},
    }
    headers = {"Authorization": f"Bearer {api_key()}", "model": model()}
    try:
        resp = httpx.post(URL, json=body, headers=headers, timeout=TIMEOUT)
    except httpx.HTTPError as exc:
        raise FishError(f"fish audio unreachable: {exc}") from exc
    if resp.status_code == 401:
        raise FishError("fish audio rejected the API key")
    if resp.status_code == 402:
        raise FishError("fish audio: payment required (out of credit)")
    if resp.status_code != 200:
        raise FishError(f"fish audio {resp.status_code}: {resp.text[:200]}")
    if not resp.content.startswith(b"RIFF"):
        raise FishError("fish audio returned something that is not a WAV")
    return resp.content
