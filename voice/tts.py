"""Text to speech, one call, one WAV: Fish Audio when a key is set, Kokoro
when ready, Piper when configured, macOS `say` otherwise. See assets.py
for the order."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from . import assets, fish, kokoro

TIMEOUT = 60.0


class TTSError(RuntimeError):
    pass


# A named system voice, e.g. "Samantha"; the system default otherwise.
def say_voice() -> str:
    return os.environ.get("AUTOPILOT_SAY_VOICE", "").strip()


def speak(text: str) -> bytes:
    text = " ".join(text.split())
    if not text:
        raise TTSError("nothing to say")
    backend = assets.tts_backend()
    if backend is None:
        raise TTSError("no local voice: install the voice extra, or use the browser voice")
    if backend == "fish":
        try:
            return fish.speak(text)
        except fish.FishError as exc:
            raise TTSError(str(exc)) from exc
    if backend == "kokoro":
        try:
            return kokoro.speak(text)
        except Exception as exc:  # noqa: BLE001 - onnxruntime and espeak raise many types
            raise TTSError(f"kokoro failed: {exc}") from exc
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "out.wav"
        if backend == "piper":
            binary = assets.piper_binary()
            cmd = [str(binary), "--model", str(assets.piper_voice()), "--output_file", str(out)]
            kwargs = {"input": text, "cwd": binary.parent}
        else:
            cmd = [str(assets.say_binary()), "-o", str(out), "--data-format=LEI16@22050"]
            if say_voice():
                cmd += ["-v", say_voice()]
            # Text as an argument, never through the shell; a leading dash
            # would read as a flag, so it goes after "--".
            cmd += ["--", text]
            kwargs = {}
        try:
            run = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT, **kwargs)
        except subprocess.TimeoutExpired as exc:
            raise TTSError(f"{backend} timed out") from exc
        if run.returncode != 0 or not out.exists():
            raise TTSError(f"{backend} failed: {(run.stderr or run.stdout).strip()[-300:]}")
        return out.read_bytes()
