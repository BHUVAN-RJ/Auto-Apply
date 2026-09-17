"""Speech endpoints for the Profile tab.

The page records 16 kHz mono PCM WAV and posts it here; it gets text back.
It posts a sentence of the reply and gets a WAV back. Status says which
half is ready and starts the downloads. When a half is not ready the page
falls back to the browser's own Web Speech API, so nothing here blocks the
interview.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

from voice import assets, stt, tts

router = APIRouter(prefix="/voice")

# A minute of 16 kHz 16-bit mono is under 2 MB; ten minutes is generous.
MAX_AUDIO_BYTES = 20 * 1024 * 1024
MAX_SPEAK_CHARS = 1500


class SpeakRequest(BaseModel):
    text: str


@router.get("/status")
def status() -> dict:
    return assets.status()


@router.post("/setup")
def setup() -> dict:
    started = assets.start_setup()
    return {"started": started} | assets.status()


@router.post("/transcribe")
async def transcribe(request: Request) -> dict:
    body = await request.body()
    if len(body) > MAX_AUDIO_BYTES:
        raise HTTPException(413, "recording too long")
    try:
        text = stt.transcribe(body)
    except stt.STTError as exc:
        raise HTTPException(503, str(exc))
    return {"text": text}


@router.post("/speak")
def speak(req: SpeakRequest) -> Response:
    text = req.text.strip()[:MAX_SPEAK_CHARS]
    if not text:
        raise HTTPException(400, "empty text")
    try:
        wav = tts.speak(text)
    except tts.TTSError as exc:
        raise HTTPException(503, str(exc))
    return Response(wav, media_type="audio/wav")
