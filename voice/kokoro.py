"""Kokoro through ONNX Runtime: the good voice.

In process rather than a subprocess: the model takes a second and a half to
load and a reply is spoken sentence by sentence, so it is loaded once and
kept. `kokoro-onnx` is an optional extra (`pip install .[voice]`); without
it, or without the model files, `available()` says why and tts.py falls
back to the system voice.

espeak-ng does the phonemes. The library bundled with `espeakng-loader`
ignores its data path on Apple Silicon and, worse, calls exit(1) from C on
the first word: it would take the server down, so it is never loaded here.
Homebrew's espeak-ng (or AUTOPILOT_ESPEAK_LIB) is required, and it is
probed once in a subprocess with a real sentence before the backend is
reported ready.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import threading
import wave
from pathlib import Path
from typing import Optional

from . import assets

MODEL_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx"
VOICES_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin"
MODEL_NAME = "kokoro-v1.0.onnx"
VOICES_NAME = "voices-v1.0.bin"

DEFAULT_VOICE = "af_heart"
ESPEAK_HINT = "brew install espeak-ng"

BREW_ESPEAK = [
    (Path("/opt/homebrew/lib/libespeak-ng.dylib"), Path("/opt/homebrew/share/espeak-ng-data")),
    (Path("/usr/local/lib/libespeak-ng.dylib"), Path("/usr/local/share/espeak-ng-data")),
]

_lock = threading.Lock()
_engine = None
_probe: Optional[str] = None  # None = untried, "" = fine, else the failure


def model_path() -> Path:
    return assets.DATA_DIR / "models" / MODEL_NAME


def voices_path() -> Path:
    return assets.DATA_DIR / "models" / VOICES_NAME


def voice() -> str:
    return os.environ.get("AUTOPILOT_KOKORO_VOICE", DEFAULT_VOICE).strip() or DEFAULT_VOICE


def installed() -> bool:
    try:
        import kokoro_onnx  # noqa: F401
    except ImportError:
        return False
    return True


def _espeak_config():
    from kokoro_onnx.config import EspeakConfig

    override = os.environ.get("AUTOPILOT_ESPEAK_LIB")
    if override:
        lib = Path(override)
        data = Path(os.environ.get("AUTOPILOT_ESPEAK_DATA", lib.parent.parent / "share" / "espeak-ng-data"))
        return EspeakConfig(lib_path=str(lib), data_path=str(data))
    for lib, data in BREW_ESPEAK:
        if lib.exists() and (data / "phontab").exists():
            return EspeakConfig(lib_path=str(lib), data_path=str(data))
    return None


def espeak_present() -> bool:
    return _espeak_config() is not None if installed() else False


def _load():
    global _engine
    from kokoro_onnx import Kokoro

    with _lock:
        if _engine is None:
            _engine = Kokoro(str(model_path()), str(voices_path()), espeak_config=_espeak_config())
    return _engine


def available() -> tuple[bool, str]:
    """(ready, reason). The probe runs one real sentence the first time,
    because the espeak failure only shows at phonemization."""
    global _probe
    if not installed():
        return False, "kokoro-onnx is not installed (pip install .[voice])"
    if not (model_path().exists() and voices_path().exists()):
        return False, "the Kokoro model is not downloaded yet"
    if not espeak_present():
        return False, f"no working espeak-ng ({ESPEAK_HINT})"
    if _probe is None:
        _probe = _probe_in_subprocess()
    return _probe == "", _probe


def _probe_in_subprocess() -> str:
    """One sentence in a child process. A bad espeak library exits the
    process it is in; better a child than the server."""
    code = "from voice import kokoro; kokoro.speak('Ready.'); print('ok')"
    try:
        run = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                             timeout=120, cwd=str(Path(__file__).resolve().parent.parent))
    except subprocess.TimeoutExpired:
        return "kokoro probe timed out"
    if run.returncode == 0 and run.stdout.strip().endswith("ok"):
        return ""
    lines = (run.stderr or run.stdout).strip().splitlines()
    return (lines[-1][:200] if lines else "") or "kokoro probe failed"


def speak(text: str, speed: float = 1.0) -> bytes:
    """One WAV, 24 kHz mono 16-bit, for one sentence or a short paragraph."""
    import numpy as np  # arrives with kokoro-onnx; not a dependency of the server

    engine = _load()
    with _lock:
        samples, rate = engine.create(text, voice=voice(), speed=speed, lang="en-us")
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16)
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return out.getvalue()
