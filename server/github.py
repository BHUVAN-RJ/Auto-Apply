"""The Projects tab's API: a GitHub handle in, ranked repositories out,
the ticked ones handed to the interviewer.

Thin over tailor/github.py. The scan runs in that module's thread; every
call here reads the state it writes, so a page poll is one cheap GET.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from tailor import github, interview

router = APIRouter(prefix="/projects")


class ScanRequest(BaseModel):
    handle: str


class PickRequest(BaseModel):
    picked: bool


class LinkRequest(BaseModel):
    link: str
    action: str = "choose"  # choose | add | remove


@router.get("")
def get_status() -> dict:
    return github.status()


@router.post("/scan")
def scan(req: ScanRequest) -> dict:
    try:
        return github.start_scan(req.handle)
    except github.GitHubError as exc:
        raise HTTPException(409 if "already running" in str(exc) else 422, str(exc))


@router.get("/{name}/scaffold")
def scaffold(name: str) -> PlainTextResponse:
    try:
        return PlainTextResponse(github.scaffold(name), media_type="text/markdown")
    except github.GitHubError as exc:
        raise HTTPException(404, str(exc))


@router.post("/{name}/pick")
def pick(name: str, req: PickRequest) -> dict:
    try:
        return github.set_picked(name, req.picked)
    except github.GitHubError as exc:
        raise HTTPException(409, str(exc))


@router.post("/{name}/link")
def link(name: str, req: LinkRequest) -> dict:
    """The addresses this project lives at: `choose` is the one the resume
    hyperlinks, `add` another address, `remove` drops one."""
    actions = {"choose": github.set_link, "add": github.add_link, "remove": github.remove_link}
    if req.action not in actions:
        raise HTTPException(422, f"no such action {req.action!r}")
    try:
        return actions[req.action](name, req.link)
    except (github.GitHubError, interview.InterviewError) as exc:
        raise HTTPException(422, str(exc))


@router.post("/confirm")
def confirm() -> dict:
    """The ticked repositories join the interview."""
    try:
        return github.confirm()
    except (github.GitHubError, interview.InterviewError) as exc:
        raise HTTPException(409, str(exc))
