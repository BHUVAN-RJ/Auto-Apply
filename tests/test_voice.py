"""Local speech: binary discovery, the subprocess wrappers, the endpoints.
No binary is run; subprocess is stubbed."""

import subprocess

import pytest
from fastapi.testclient import TestClient

from server.app import app
from voice import assets, fish, kokoro, stt, tts


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(assets, "DATA_DIR", tmp_path / "voice")
    monkeypatch.delenv("AUTOPILOT_WHISPER_BIN", raising=False)
    monkeypatch.delenv("AUTOPILOT_PIPER_BIN", raising=False)
    monkeypatch.delenv("AUTOPILOT_SAY_VOICE", raising=False)
    monkeypatch.delenv("AUTOPILOT_FISH_API_KEY", raising=False)
    monkeypatch.delenv("AUTOPILOT_FISH_MODEL", raising=False)
    monkeypatch.delenv("AUTOPILOT_FISH_VOICE", raising=False)
    for name in ("AUTOPILOT_FISH_EMOTION", "AUTOPILOT_FISH_TEMPERATURE", "AUTOPILOT_FISH_SPEED"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(assets.shutil, "which", lambda name: None)
    monkeypatch.setattr(assets, "BREW_PREFIXES", ())  # a real brew whisper-cli must not leak in
    monkeypatch.setattr(assets, "_thread", None)
    # No system `say` unless a test installs one: Linux CI has none.
    monkeypatch.setattr(assets.platform, "system", lambda: "Linux")
    # Kokoro absent unless a test says otherwise; never probe for real.
    monkeypatch.setattr(kokoro, "installed", lambda: False)
    monkeypatch.setattr(kokoro, "_probe", None)
    monkeypatch.setattr(kokoro, "_probe_in_subprocess", lambda: pytest.fail("real kokoro probe"))
    monkeypatch.setattr(kokoro, "BREW_ESPEAK", [])
    monkeypatch.delenv("AUTOPILOT_ESPEAK_LIB", raising=False)


def install_everything(tmp_path, monkeypatch):
    (tmp_path / "voice" / "models").mkdir(parents=True)
    assets.whisper_model().write_bytes(b"model")
    (tmp_path / "voice" / "bin").mkdir()
    (tmp_path / "voice" / "bin" / "whisper-cli").write_text("#!/bin/sh\n")
    piper = tmp_path / "piper"
    piper.write_text("#!/bin/sh\n")
    monkeypatch.setenv("AUTOPILOT_PIPER_BIN", str(piper))
    assets.piper_voice().write_bytes(b"voice")
    assets.piper_voice().with_suffix(".onnx.json").write_text("{}")


def test_status_reports_what_is_missing_and_the_brew_hint(tmp_path, monkeypatch):
    data = assets.status()
    assert data["stt"]["ready"] is False and data["stt"]["install"] == "brew install whisper-cpp"
    assert data["tts"]["backend"] is None and data["tts"]["ready"] is False
    assert "not installed" in data["tts"]["kokoro"]
    assert data["missing"] == ["whisper model"]
    install_everything(tmp_path, monkeypatch)
    data = assets.status()
    assert data["stt"]["ready"] and data["tts"]["backend"] == "piper"
    assert data["missing"] == [] and data["stt"]["install"] is None


def test_macos_say_is_the_default_voice(tmp_path, monkeypatch):
    monkeypatch.setattr(assets.platform, "system", lambda: "Darwin")
    say = tmp_path / "say"
    say.write_text("")
    monkeypatch.setattr(assets.shutil, "which", lambda name: str(say) if name == "say" else None)
    assert assets.tts_backend() == "say"
    # The voice files are only the downloader's business once Piper is configured.
    assert [a.name for a in assets.assets()] == ["whisper model"]


def test_the_python_whisper_cli_is_not_mistaken_for_whisper_cpp(monkeypatch):
    monkeypatch.setattr(assets.shutil, "which",
                        lambda name: "/usr/bin/whisper" if name == "whisper" else None)
    monkeypatch.setattr(assets.Path, "exists", lambda self: False)
    assert assets.whisper_binary() is None


def test_transcribe_runs_whisper_cli_on_a_wav_and_reads_the_txt(tmp_path, monkeypatch):
    install_everything(tmp_path, monkeypatch)
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        prefix = cmd[cmd.index("-of") + 1]
        (tmp_path / "unused").mkdir(exist_ok=True)
        with open(prefix + ".txt", "w") as f:
            f.write(" I built the  billing service.\n")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(stt.subprocess, "run", fake_run)
    assert stt.transcribe(b"x" * 2000) == "I built the billing service."
    assert seen["cmd"][0].endswith("whisper-cli") and "-otxt" in seen["cmd"]


def test_transcribe_without_binary_or_model_says_which(tmp_path):
    with pytest.raises(stt.STTError, match="brew install"):
        stt.transcribe(b"x" * 2000)
    (tmp_path / "voice" / "bin").mkdir(parents=True)
    (tmp_path / "voice" / "bin" / "whisper-cli").write_text("")
    with pytest.raises(stt.STTError, match="model"):
        stt.transcribe(b"x" * 2000)


def test_tiny_recording_is_silence(tmp_path, monkeypatch):
    install_everything(tmp_path, monkeypatch)
    monkeypatch.setattr(stt.subprocess, "run", lambda *a, **k: pytest.fail("whisper ran on nothing"))
    assert stt.transcribe(b"RIFF") == ""


def test_speak_pipes_text_into_piper_and_returns_the_wav(tmp_path, monkeypatch):
    install_everything(tmp_path, monkeypatch)

    def fake_run(cmd, **kwargs):
        assert kwargs["input"] == "Tell me the story."
        with open(cmd[cmd.index("--output_file") + 1], "wb") as f:
            f.write(b"RIFFwav")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(tts.subprocess, "run", fake_run)
    assert tts.speak("Tell  me the\nstory.") == b"RIFFwav"


def test_speak_failure_is_an_error(tmp_path, monkeypatch):
    install_everything(tmp_path, monkeypatch)
    monkeypatch.setattr(tts.subprocess, "run",
                        lambda cmd, **k: subprocess.CompletedProcess(cmd, 1, "", "bad phoneme"))
    with pytest.raises(tts.TTSError, match="bad phoneme"):
        tts.speak("hi")


def test_speak_with_say_passes_text_as_an_argument_after_a_dash_dash(tmp_path, monkeypatch):
    monkeypatch.setattr(assets.platform, "system", lambda: "Darwin")
    say = tmp_path / "say"
    say.write_text("")
    monkeypatch.setattr(assets.shutil, "which", lambda name: str(say) if name == "say" else None)
    monkeypatch.setenv("AUTOPILOT_SAY_VOICE", "Samantha")

    def fake_run(cmd, **kwargs):
        assert cmd[0] == str(say) and cmd[-2:] == ["--", "-dash first."]
        assert "-v" in cmd and cmd[cmd.index("-v") + 1] == "Samantha"
        assert "input" not in kwargs
        with open(cmd[cmd.index("-o") + 1], "wb") as f:
            f.write(b"RIFFsay")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(tts.subprocess, "run", fake_run)
    assert tts.speak("-dash first.") == b"RIFFsay"


def test_endpoints(tmp_path, monkeypatch):
    client = TestClient(app)
    assert client.get("/voice/status").json()["stt"]["ready"] is False
    assert client.post("/voice/transcribe", content=b"x" * 2000).status_code == 503
    assert client.post("/voice/speak", json={"text": "hi"}).status_code == 503
    assert client.post("/voice/speak", json={"text": "  "}).status_code == 400
    install_everything(tmp_path, monkeypatch)
    monkeypatch.setattr(stt, "transcribe", lambda wav: "hello there")
    monkeypatch.setattr(tts, "speak", lambda text: b"RIFF" + text.encode())
    assert client.post("/voice/transcribe", content=b"x" * 2000).json() == {"text": "hello there"}
    response = client.post("/voice/speak", json={"text": "hi"})
    assert response.headers["content-type"] == "audio/wav" and response.content == b"RIFFhi"


def test_setup_downloads_only_what_is_missing(tmp_path, monkeypatch):
    install_everything(tmp_path, monkeypatch)
    assets.whisper_model().unlink()
    downloaded = []

    def fake_download(asset, report=None):
        downloaded.append(asset.name)
        asset.dest.write_bytes(b"m")
        if report:
            report(1, 1)

    monkeypatch.setattr(assets, "download", fake_download)
    assert assets.start_setup() is True
    assets._thread.join(5)
    assert downloaded == ["whisper model"]
    assert assets.progress()["whisper model"]["state"] == "done"
    assert assets.missing() == []


def kokoro_installed(tmp_path, monkeypatch, espeak=True):
    monkeypatch.setattr(kokoro, "installed", lambda: True)
    (tmp_path / "voice" / "models").mkdir(parents=True, exist_ok=True)
    kokoro.model_path().write_bytes(b"onnx")
    kokoro.voices_path().write_bytes(b"voices")
    if espeak:
        lib = tmp_path / "lib" / "libespeak-ng.dylib"
        data = tmp_path / "share" / "espeak-ng-data"
        lib.parent.mkdir(); data.mkdir(parents=True)
        lib.write_text(""); (data / "phontab").write_text("")
        monkeypatch.setattr(kokoro, "BREW_ESPEAK", [(lib, data)])
        monkeypatch.setattr(kokoro, "EspeakConfig", None, raising=False)


def test_kokoro_wins_when_installed_downloaded_and_espeak_works(tmp_path, monkeypatch):
    kokoro_installed(tmp_path, monkeypatch)
    monkeypatch.setattr(kokoro, "_espeak_config", lambda: ("lib", "data"))
    monkeypatch.setattr(kokoro, "_probe_in_subprocess", lambda: "")
    assert assets.tts_backend() == "kokoro"
    monkeypatch.setattr(kokoro, "speak", lambda text: b"RIFFkokoro")
    assert tts.speak("hi") == b"RIFFkokoro"
    # The model files join the download list once the package is present.
    assert [a.name for a in assets.assets()] == ["whisper model", "kokoro model", "kokoro voices"]


def test_kokoro_without_a_working_espeak_is_reported_not_used(tmp_path, monkeypatch):
    kokoro_installed(tmp_path, monkeypatch, espeak=False)
    monkeypatch.setattr(kokoro, "_espeak_config", lambda: None)
    ready, reason = kokoro.available()
    assert not ready and "brew install espeak-ng" in reason
    assert assets.tts_backend() is None
    assert assets.status()["tts"]["kokoro"] == reason


def test_kokoro_probe_failure_falls_back(tmp_path, monkeypatch):
    kokoro_installed(tmp_path, monkeypatch)
    monkeypatch.setattr(kokoro, "_espeak_config", lambda: ("lib", "data"))
    monkeypatch.setattr(kokoro, "_probe_in_subprocess", lambda: "Error processing file phontab")
    assert kokoro.available() == (False, "Error processing file phontab")
    monkeypatch.setattr(assets.platform, "system", lambda: "Darwin")
    say = tmp_path / "say"; say.write_text("")
    monkeypatch.setattr(assets.shutil, "which", lambda name: str(say) if name == "say" else None)
    assert assets.tts_backend() == "say"


def test_kokoro_model_missing_is_the_reason(tmp_path, monkeypatch):
    monkeypatch.setattr(kokoro, "installed", lambda: True)
    assert kokoro.available() == (False, "the Kokoro model is not downloaded yet")


class FakeResponse:
    def __init__(self, status_code=200, content=b"RIFFfish", text=""):
        self.status_code, self.content, self.text = status_code, content, text


def test_fish_wins_over_every_local_voice_when_the_key_is_set(tmp_path, monkeypatch):
    kokoro_installed(tmp_path, monkeypatch)
    monkeypatch.setattr(kokoro, "_espeak_config", lambda: ("lib", "data"))
    monkeypatch.setattr(kokoro, "_probe_in_subprocess", lambda: "")
    monkeypatch.setenv("AUTOPILOT_FISH_API_KEY", "k")
    monkeypatch.setenv("AUTOPILOT_FISH_VOICE", "v1")
    assert assets.tts_backend() == "fish"
    seen = {}

    def post(url, json, headers, timeout):
        seen.update(url=url, json=json, headers=headers)
        return FakeResponse()
    monkeypatch.setattr(fish.httpx, "post", post)
    assert tts.speak("hi  there") == b"RIFFfish"
    assert seen["url"] == fish.URL
    assert seen["headers"] == {"Authorization": "Bearer k", "model": "s2.1-pro-free"}
    assert seen["json"]["text"] == "(cheerful) hi there" and seen["json"]["format"] == "wav"
    assert seen["json"]["temperature"] == 0.9 and seen["json"]["prosody"] == {"speed": 1.08}
    assert seen["json"]["reference_id"] == "v1"
    status = assets.status()["tts"]
    assert status["backend"] == "fish" and status["kokoro"] is None
    assert status["fish"] == {"model": "s2.1-pro-free", "voice": "v1"}


def test_fish_errors_become_tts_errors_and_no_key_means_no_fish(monkeypatch):
    assert assets.tts_backend() is None
    monkeypatch.setenv("AUTOPILOT_FISH_API_KEY", "k")
    monkeypatch.setenv("AUTOPILOT_FISH_MODEL", "s1")
    for resp, needle in [(FakeResponse(401), "API key"), (FakeResponse(402), "payment"),
                         (FakeResponse(503, text="busy"), "503"), (FakeResponse(200, b"<html>"), "not a WAV")]:
        monkeypatch.setattr(fish.httpx, "post", lambda *a, **k: resp)
        with pytest.raises(tts.TTSError, match=needle):
            tts.speak("hi")
    seen = {}
    monkeypatch.setattr(fish.httpx, "post", lambda url, json, headers, timeout: seen.update(headers=headers, json=json) or FakeResponse())
    tts.speak("hi")
    assert seen["headers"]["model"] == "s1" and seen["json"]["reference_id"] == fish.DEFAULT_VOICE

    def down(*a, **k):
        raise fish.httpx.ConnectError("no route")
    monkeypatch.setattr(fish.httpx, "post", down)
    with pytest.raises(tts.TTSError, match="unreachable"):
        tts.speak("hi")


def test_fish_delivery_is_tunable_and_the_tag_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("AUTOPILOT_FISH_API_KEY", "k")
    monkeypatch.setenv("AUTOPILOT_FISH_EMOTION", "")
    monkeypatch.setenv("AUTOPILOT_FISH_SPEED", "1.3")
    monkeypatch.setenv("AUTOPILOT_FISH_TEMPERATURE", "junk")
    seen = {}
    monkeypatch.setattr(fish.httpx, "post", lambda url, json, headers, timeout: seen.update(json=json) or FakeResponse())
    tts.speak("hi")
    assert seen["json"]["text"] == "hi"
    assert seen["json"]["prosody"] == {"speed": 1.3}
    assert seen["json"]["temperature"] == fish.DEFAULT_TEMPERATURE


def test_whisper_non_speech_markers_are_silence():
    assert stt.clean("[BLANK_AUDIO]") == ""
    assert stt.clean(" (upbeat music) ") == ""
    assert stt.clean("I built the [inaudible] service.") == "I built the service."
    assert stt.clean("Plain  answer\n here") == "Plain answer here"


def test_whisper_fillers_and_stutters_are_dropped():
    assert stt.clean("Um, so I, uh, built the the ledger, hmm, in Go.") == "so I built the ledger in Go."
    assert stt.clean("Ah I think erm it was about, um, four people.") == "I think it was about four people."
    # Words that contain the sounds are not filler.
    assert stt.clean("The album summed the humdrum uhuru data.") == "The album summed the humdrum uhuru data."
    assert stt.clean("Uh-huh, yes.") == "Uh-huh, yes."
