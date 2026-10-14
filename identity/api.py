"""A router, not an app.

Both the server on 8787 and anything else that wants the same answer mount
this; nothing here starts a server or wires itself up anywhere. Every
endpoint is a lookup over the index - nothing in this package touches the
queue, the application folders or a status transition.

    from identity.api import router
    app.include_router(router)
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

import identity

router = APIRouter(prefix="/identity", tags=["identity"])


class Ask(BaseModel):
    url: str
    title: str = ""
    company: str = ""
    location: str = ""
    text: str = ""
    # A caller that cannot afford the (about 0.3 s) catch-up read can turn
    # it off; the answer then says which sources were behind.
    refresh: bool = True
    explain: bool = False


@router.post("/lookup")
def lookup(ask: Ask) -> dict:
    if ask.explain:
        return identity.explain(ask.url, ask.title, ask.company, ask.location, ask.text)
    return identity.lookup(ask.url, ask.title, ask.company, ask.location,
                           ask.text, refresh=ask.refresh).to_dict()


@router.post("/backfill")
def backfill() -> dict:
    """Rebuild the index from the queue and the applications folder.

    Idempotent and read-only on the data; safe to call whenever.
    """
    return identity.backfill()


@router.get("/stats")
def stats() -> dict:
    return identity.stats()


@router.get("/collisions")
def collisions() -> dict:
    """Short four-hex job ids that name more than one job (KNOWN_ISSUES #1)."""
    found = identity.collisions()
    return {"collisions": found, "count": len(found)}
