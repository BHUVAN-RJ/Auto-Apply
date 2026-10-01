"""LinkedIn Easy Apply, walked without a browser.

What matters is where it stops. It never presses Submit, it stops the moment
a page asks something only the person can answer, and the tailored resume
goes on exactly once.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from browser import linkedin_apply as li


def page(n, total, heads, buttons=("Next",), empty=0, files=0, asked=(), editable=4):
    return {
        "url": "https://www.linkedin.com/jobs/view/1/",
        "text": f"Apply to ZealTech\n{n}/{total} pages\n" + "\n".join(heads),
        "heads": list(heads),
        "buttons": [{"text": b, "disabled": False, "x": 10, "y": 20, "top": 400} for b in buttons],
        "editable": editable, "files": files, "empty": empty,
        "asked": [{"q": q, "answered": False, "type": "text"} for q in asked],
    }


def test_the_flow_is_recognised_by_what_it_prints():
    assert li.in_flow(page(1, 5, ["Contact info"]))
    assert li.progress(page(2, 5, ["Resume"])) == "2/5"
    assert not li.in_flow({"text": "Ai Analytics Engineer\nZealTech\nAbout the job"})


def test_the_stages():
    assert li.stage_of(page(1, 5, ["Contact info"])) == "form"
    assert li.stage_of(page(2, 5, ["Resume"], files=1)) == "resume"
    assert li.stage_of(page(3, 5, ["Additional Questions"], empty=2,
                            asked=("How many years of Python?",))) == "questions"
    assert li.stage_of(page(5, 5, ["Review your application"],
                            buttons=("Submit application",))) == "review"
    assert li.stage_of({"text": "a job description", "heads": [], "buttons": []}) == "away"


def test_submit_is_never_the_next_button():
    look = page(5, 5, ["Review"], buttons=("Submit application", "Back"))
    assert li.next_button(look) is None
    assert li.next_button(page(1, 5, ["Contact info"], buttons=("Submit application", "Next")))["text"] == "Next"


class Flow:
    """A scripted Easy Apply: each look is served until something is pressed."""

    def __init__(self, looks, uploads=True):
        self.looks = list(looks); self.at = 0
        self.clicks = []; self.uploads = uploads; self.files = []

    async def evaluate(self, expression):
        if "elementFromPoint" in expression:
            return True
        if "data-autopilot-ref" in expression and "input[type=file]" in expression:
            return {"name": ""} if self.looks[self.at].get("files") else None
        if "li-resume" in expression:
            return "resume.pdf" if self.uploads else ""
        return self.looks[min(self.at, len(self.looks) - 1)]

    async def send(self, method, params=None):
        if method == "Input.dispatchMouseEvent" and (params or {}).get("type") == "mousePressed":
            self.clicks.append(params); self.at = min(self.at + 1, len(self.looks) - 1)
        if method == "Runtime.evaluate":
            return {"result": {"objectId": "1"}}
        if method == "DOM.describeNode":
            return {"node": {"backendNodeId": 7}}
        if method == "DOM.setFileInputFiles":
            self.files.append(params["files"][0])
        return {}


@pytest.fixture
def flow(monkeypatch):
    def build(looks, uploads=True):
        f = Flow(looks, uploads)

        @asynccontextmanager
        async def attached(cdp_url, url="", target_id=""):
            yield f, target_id or "TAB"
        import browser.autofill as autofill_mod
        monkeypatch.setattr(autofill_mod, "attached", attached)
        monkeypatch.setattr(li, "SETTLE", 0)
        monkeypatch.setattr(li, "ADVANCE_TIMEOUT", 0.05)
        return f
    return build


def walk(**kw):
    return asyncio.run(li.walk("http://x", "TAB", **kw))


def test_it_puts_the_resume_on_and_stops_at_the_review(flow, tmp_path):
    resume = tmp_path / "resume.pdf"; resume.write_bytes(b"%PDF")
    f = flow([
        page(1, 4, ["Contact info"]),
        page(2, 4, ["Resume"], files=1),
        page(3, 4, ["Additional Questions"]),
        page(4, 4, ["Review your application"], buttons=("Submit application",)),
    ])
    result = walk(resume=resume)
    assert result.reached_review and not result.paused
    assert result.resume_uploaded and f.files == [str(resume.resolve())]
    assert result.ok
    # Nothing on the review page was pressed.
    assert len(f.clicks) == 3


def test_it_stops_at_a_question_only_the_person_can_answer(flow, tmp_path):
    resume = tmp_path / "resume.pdf"; resume.write_bytes(b"%PDF")
    flow([
        page(1, 4, ["Contact info"]),
        page(2, 4, ["Resume"], files=1),
        page(3, 4, ["Additional Questions"], empty=2,
             asked=("How many years of experience do you have with Python?",
                    "Are you legally authorized to work in the United States?")),
    ])
    result = walk(resume=resume)
    assert result.paused == "questions"
    assert "years of experience" in result.asks[0]
    assert result.resume_uploaded, "the resume still went on before it stopped"


def test_a_page_that_is_not_the_flow_stops_it(flow):
    flow([{"text": "Ai Analytics Engineer\nAbout the job", "heads": [], "buttons": [],
           "editable": 0, "files": 0, "empty": 0, "asked": []}])
    result = walk()
    assert result.paused == "not_in_the_flow"


def test_the_resume_goes_on_once(flow, tmp_path):
    resume = tmp_path / "resume.pdf"; resume.write_bytes(b"%PDF")
    f = flow([
        page(1, 3, ["Resume"], files=1),
        page(2, 3, ["Resume"], files=1),
        page(3, 3, ["Review"], buttons=("Submit application",)),
    ])
    result = walk(resume=resume)
    assert result.reached_review and len(f.files) == 1
