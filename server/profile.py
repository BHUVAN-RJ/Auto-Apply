"""The Profile tab's API: the interviewer and its documents.

Thin over tailor/interview.py. The one thing this file adds is the wire
format: a turn is a server-sent event stream, one JSON event per line, so
the page can print the reply as it is written and the voice layer can
start speaking on the first sentence.
"""

from __future__ import annotations

import json
from typing import Iterator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel

from tailor import interview, llm

router = APIRouter(prefix="/profile")


class StartRequest(BaseModel):
    seed: str = ""


class TurnRequest(BaseModel):
    message: str


@router.get("")
def get_status() -> dict:
    return interview.status()


@router.post("/start")
def start(req: StartRequest) -> dict:
    try:
        return interview.start(req.seed)
    except (interview.InterviewError, llm.LLMError) as exc:
        raise HTTPException(409 if "already exists" in str(exc) else 502, str(exc))


# An old resume is a few hundred KB at most; a scanned one is images and
# has no text to extract anyway.
MAX_SEED_FILE_BYTES = 20 * 1024 * 1024


@router.post("/seed-file")
async def seed_file(request: Request) -> dict:
    """Text of one picked file, so the page can add it to the seed box.
    Raw body plus an X-Filename header: no multipart dependency."""
    name = request.headers.get("x-filename", "file")
    data = await request.body()
    if len(data) > MAX_SEED_FILE_BYTES:
        raise HTTPException(413, f"{name} is too large")
    try:
        return {"name": name, "text": interview.extract_text(name, data)}
    except interview.InterviewError as exc:
        raise HTTPException(422, str(exc))


def _events(message: str) -> Iterator[str]:
    for event in interview.turn(message):
        yield f"data: {json.dumps(event)}\n\n"


@router.post("/turn")
def turn(req: TurnRequest) -> StreamingResponse:
    if not req.message.strip():
        raise HTTPException(400, "empty message")
    return StreamingResponse(_events(req.message), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/experiences/{slug}/{name}")
def document(slug: str, name: str) -> PlainTextResponse:
    try:
        return PlainTextResponse(interview.document(slug, name), media_type="text/markdown")
    except interview.InterviewError as exc:
        raise HTTPException(404, str(exc))


@router.post("/experiences/{slug}/regenerate")
def regenerate(slug: str) -> dict:
    """Rewrite tailor.md and star.md from main.md. For a failed generation,
    or after story_rules.md changed."""
    try:
        experience = interview.Experience.load(slug)
    except interview.InterviewError as exc:
        raise HTTPException(404, str(exc))
    if not experience.closed:
        raise HTTPException(409, "the interview for this experience is not finished")
    interview.start_children(slug)
    return {"slug": slug, "children": "pending"}
