"""Speech to text through whisper.cpp's command line.

Input is a WAV the page recorded: 16 kHz, mono, 16-bit PCM, which is what
whisper-cli wants without a resampler, so ffmpeg is not a dependency.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path

from . import assets

TIMEOUT = 120.0


class STTError(RuntimeError):
    pass


def transcribe(wav: bytes, language: str = "en") -> str:
    binary = assets.whisper_binary()
    if binary is None:
        raise STTError(f"whisper.cpp is not installed ({assets.BREW_HINT})")
    model = assets.whisper_model()
    if not model.exists():
        raise STTError("the whisper model is not downloaded yet")
    if len(wav) < 1000:
        return ""
    with tempfile.TemporaryDirectory() as tmp:
        audio = Path(tmp) / "in.wav"
        audio.write_bytes(wav)
        prefix = Path(tmp) / "out"
        cmd = [str(binary), "-m", str(model), "-f", str(audio), "-l", language,
               "-nt", "-np", "-otxt", "-of", str(prefix)]
        try:
            run = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT)
        except subprocess.TimeoutExpired as exc:
            raise STTError("whisper timed out") from exc
        if run.returncode != 0:
            raise STTError(f"whisper failed: {(run.stderr or run.stdout).strip()[-300:]}")
        out = prefix.with_suffix(".txt")
        text = out.read_text() if out.exists() else run.stdout
    return clean(text)


# whisper.cpp names what it could not transcribe: "[BLANK_AUDIO]",
# "(upbeat music)", "[inaudible]". Those are silence, not an answer; a
# headless run once sent "[BLANK_AUDIO]" into the interview as one.
NON_SPEECH = re.compile(r"[\[(][^\])]*[\])]")
# Filler the candidate would rather not read back: standalone hesitation
# sounds and their trailing comma, and an immediately repeated word ("the
# the"). "Like" and "you know" stay; they carry meaning too often.
FILLER = re.compile(r"(?:,\s*)?(?<![\w'-])(?:u+m+|u+h+|u+h+m+|e+r+m*|a+h+|h+m+m*|mm+)[,.]?(?![\w'-])", re.I)
STUTTER = re.compile(r"\b(\w+)(?:[,]?\s+\1\b)+", re.I)


def clean(text: str) -> str:
    text = NON_SPEECH.sub(" ", text)
    text = FILLER.sub(" ", text)
    text = STUTTER.sub(r"\1", text)
    text = re.sub(r"\s+([,.?!])", r"\1", text)
    text = re.sub(r"^[,.\s]+", "", text)
    return " ".join(text.split())
