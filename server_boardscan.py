"""The board scan's own server, on its own port.

Separate from autopilot on purpose, and temporarily: this collects and
lists jobs, autopilot applies for them, and the two are meant to be one
app once the collection half has earned it. Keeping them apart now means
a crawl that goes wrong cannot touch the process that owns the fill queue,
and the page can be rebuilt without reloading the one the applications are
reviewed on.

It reads autopilot's data (the queue, the applications) to answer "have I
applied for this already" and writes none of it. Nothing here fills a
form, presses a button or opens a tab.

    uv run uvicorn server_boardscan:app --port 8788
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, Response

import paths
from boardscan.api import router

paths.load_env()

PAGE = Path(__file__).resolve().parent / "boardscan" / "index.html"

app = FastAPI(title="Board scan")
app.include_router(router)


@app.get("/")
def page() -> Response:
    """No-cache, for the same reason the review page is served that way:
    Chrome kept an old copy of it for a whole session once."""
    return FileResponse(PAGE, media_type="text/html",
                        headers={"Cache-Control": "no-cache"})
