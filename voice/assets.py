"""Where the speech binaries and models live, and how they get there.

Everything under the app data directory, next to the Chrome profile. The
whisper model is a plain download; whisper.cpp publishes no macOS binary,
so until the app ships one it comes from Homebrew or PATH, and `status()`
says so rather than failing later in a transcription.

Speech out is Kokoro (voice/kokoro.py) when its package and model are
present, macOS `say` otherwise: on-device, installed everywhere, nothing
to download, and the voice is why Kokoro exists. Piper was the first plan,
but on an Apple Silicon Mac neither route works today: the 2023.11.14-2
release's "macos_aarch64" tarball is an x86_64 build, and the `piper-tts`
wheel's espeak bridge ignores its data path and fails on every phoneme. Set
AUTOPILOT_PIPER_BIN to a working binary to use it; the voice files are
downloaded alongside the whisper model when that is set.
"""

from __future__ import annotations

import os
import platform
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import httpx

from . import fish, kokoro

DATA_DIR = Path(os.environ.get(
    "AUTOPILOT_DATA_DIR",
    Path.home() / "Library" / "Application Support" / "job-autopilot")) / "voice"

# Small English models: fast enough on a laptop CPU, good enough for one
# speaker in a quiet room. Bigger ones are a URL change here.
WHISPER_MODEL_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin"
WHISPER_MODEL = "ggml-base.en.bin"

PIPER_VOICE_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/lessac/medium"
PIPER_VOICE = "en_US-lessac-medium.onnx"

# Not plain "whisper": that is the openai-whisper Python CLI, torch and all,
# with different flags.
WHISPER_BINARY_NAMES = ("whisper-cli", "whisper-cpp")
BREW_HINT = "brew install whisper-cpp"
# Checked after PATH: the server may be launched from an app with no shell profile.
BREW_PREFIXES = ("/opt/homebrew/bin", "/usr/local/bin")


@dataclass
class Asset:
    name: str
    url: str
    dest: Path

    def present(self) -> bool:
        return self.dest.exists() and self.dest.stat().st_size > 0


def piper_binary() -> Optional[Path]:
    override = os.environ.get("AUTOPILOT_PIPER_BIN")
    if override and Path(override).exists():
        return Path(override)
    return None


def say_binary() -> Optional[Path]:
    if platform.system() != "Darwin":
        return None
    found = shutil.which("say") or "/usr/bin/say"
    return Path(found) if Path(found).exists() else None


def whisper_binary() -> Optional[Path]:
    override = os.environ.get("AUTOPILOT_WHISPER_BIN")
    if override:
        return Path(override) if Path(override).exists() else None
    bundled = DATA_DIR / "bin" / "whisper-cli"
    if bundled.exists():
        return bundled
    for name in WHISPER_BINARY_NAMES:
        found = shutil.which(name)
        if found:
            return Path(found)
    for prefix in BREW_PREFIXES:
        for name in WHISPER_BINARY_NAMES:
            candidate = Path(prefix) / name
            if candidate.exists():
                return candidate
    return None


def whisper_model() -> Path:
    return DATA_DIR / "models" / WHISPER_MODEL


def piper_voice() -> Path:
    return DATA_DIR / "models" / PIPER_VOICE


def assets() -> list[Asset]:
    """Everything the downloader is responsible for. The whisper binary is
    deliberately not here (see the module docstring); the Piper voice only
    when a Piper binary is configured."""
    items = [Asset("whisper model", WHISPER_MODEL_URL, whisper_model())]
    if kokoro.installed():
        items += [
            Asset("kokoro model", kokoro.MODEL_URL, kokoro.model_path()),
            Asset("kokoro voices", kokoro.VOICES_URL, kokoro.voices_path()),
        ]
    if piper_binary():
        items += [
            Asset("voice", f"{PIPER_VOICE_BASE}/{PIPER_VOICE}", piper_voice()),
            Asset("voice config", f"{PIPER_VOICE_BASE}/{PIPER_VOICE}.json",
                  piper_voice().with_suffix(".onnx.json")),
        ]
    return items


def missing() -> list[Asset]:
    return [a for a in assets() if not a.present()]


# ------------------------------------------------------------- download --

_progress: dict = {}
_lock = threading.Lock()
_thread: Optional[threading.Thread] = None


def progress() -> dict:
    with _lock:
        return {k: dict(v) for k, v in _progress.items()}


def download(asset: Asset, report: Optional[Callable[[int, int], None]] = None) -> None:
    asset.dest.parent.mkdir(parents=True, exist_ok=True)
    partial = asset.dest.with_suffix(asset.dest.suffix + ".part")
    with httpx.stream("GET", asset.url, follow_redirects=True, timeout=120.0) as response:
        response.raise_for_status()
        total = int(response.headers.get("content-length") or 0)
        done = 0
        with partial.open("wb") as out:
            for chunk in response.iter_bytes(1 << 16):
                out.write(chunk)
                done += len(chunk)
                if report:
                    report(done, total)
    partial.replace(asset.dest)


def _setup_all() -> None:
    for asset in missing():
        with _lock:
            _progress[asset.name] = {"done": 0, "total": 0, "state": "downloading"}

        def report(done: int, total: int, name: str = asset.name) -> None:
            with _lock:
                _progress[name].update(done=done, total=total)

        try:
            download(asset, report)
            with _lock:
                _progress[asset.name]["state"] = "done"
        except Exception as exc:  # noqa: BLE001 - shown on the page
            with _lock:
                _progress[asset.name].update(state="error", error=f"{type(exc).__name__}: {exc}")


def start_setup() -> bool:
    """Download what is missing, in the background. Returns False when a
    download is already running."""
    global _thread
    with _lock:
        if _thread and _thread.is_alive():
            return False
        _progress.clear()
        _thread = threading.Thread(target=_setup_all, daemon=True)
        _thread.start()
        return True


def setup_running() -> bool:
    return bool(_thread and _thread.is_alive())


def tts_backend() -> Optional[str]:
    """'fish' when AUTOPILOT_FISH_API_KEY is set, 'kokoro' when ready, 'piper'
    when configured and its voice is present, else 'say' on macOS, else None
    (the page uses the browser's own voice)."""
    if fish.configured():
        return "fish"
    if kokoro.available()[0]:
        return "kokoro"
    if piper_binary() and piper_voice().exists():
        return "piper"
    if say_binary():
        return "say"
    return None


def status() -> dict:
    whisper = whisper_binary()
    backend = tts_backend()
    return {
        "stt": {
            "binary": str(whisper) if whisper else None,
            "model": whisper_model().exists(),
            "ready": bool(whisper) and whisper_model().exists(),
            "install": None if whisper else BREW_HINT,
        },
        "tts": {
            "backend": backend,
            "ready": backend is not None,
            # Why the good voice is not the one in use, when it is not.
            "kokoro": None if backend in ("fish", "kokoro") else kokoro.available()[1],
            "fish": {"model": fish.model(), "voice": fish.reference_id()} if backend == "fish" else None,
        },
        "missing": [a.name for a in missing()],
        "downloading": setup_running(),
        "progress": progress(),
        "data_dir": str(DATA_DIR),
    }
