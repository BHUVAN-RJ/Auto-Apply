"""The profile interviewer: state on disk, header parsing, the ten-question
cap, documents written on close. The model is a scripted stub."""

import json

import pytest
from fastapi.testclient import TestClient

from server import settings
from server.app import app
from tailor import interview, profile

RESUME = r"""\documentclass{article}
\begin{document}
\section{Experience}
Acme Corp, Backend Intern, Summer 2025. Built the billing service.
\section{Projects}
Hydra: a distributed cache in Go.
\end{document}
"""

EXTRACTION = json.dumps({
    "experienced": False,
    "experiences": [
        {"title": "Backend Intern, Acme Corp", "kind": "role",
         "resume_entry": "Acme Corp, Backend Intern, Summer 2025. Built the billing service."},
        {"title": "Hydra", "kind": "project", "resume_entry": "Hydra: a distributed cache in Go."},
    ],
})


class Script:
    """Replies in order; each `stream` call yields the next reply in pieces
    so the header split is exercised across chunk boundaries."""

    def __init__(self):
        self.replies: list[str] = []
        self.calls: list[list[dict]] = []
        self.completes: list[tuple[str, str]] = []

    def stream(self, messages, **kwargs):
        self.calls.append(messages)
        reply = self.replies.pop(0)
        for i in range(0, len(reply), 7):
            yield reply[i:i + 7]

    def complete(self, system, user, **kwargs):
        self.completes.append((system, user))
        return self.replies.pop(0)


@pytest.fixture
def script(tmp_path, monkeypatch):
    stories = tmp_path / "stories"
    monkeypatch.setattr(profile, "STORIES", stories)
    monkeypatch.setattr(profile, "BASE_RESUME", tmp_path / "resume.tex")
    monkeypatch.setattr(settings, "SETTINGS_PATH", tmp_path / "settings.json")
    (tmp_path / "resume.tex").write_text(RESUME)
    s = Script()
    monkeypatch.setattr(interview.llm, "stream", s.stream)
    monkeypatch.setattr(interview.llm, "complete", s.complete)
    # Children run the heavy model in a thread; tests never start it.
    started = []
    monkeypatch.setattr(interview, "start_children", lambda slug: started.append(slug))
    s.started = started
    return s


def events(message):
    return list(interview.turn(message))


def shown(evs):
    return "".join(e["text"] for e in evs if e["type"] == "delta")


def begin(script):
    script.replies.append("```json\n" + EXTRACTION + "\n```")
    result = interview.start("")
    script.replies.append("EXPERIENCES: []\nSTART: yes\n---\nGood, let's start.")
    script.replies.append("COVERED:\nDONE: no\n---\nTell me the story of the billing service.")
    return result, events("Looks right.")


def test_start_lists_roles_and_projects_and_asks_what_is_missing(script):
    result, _ = begin(script)
    message = result["message"]
    assert "Roles:" in message and "Backend Intern, Acme Corp" in message
    assert "Projects:" in message and "Hydra" in message
    assert "missing" in message
    assert result["status"]["phase"] in ("setup", "interviewing")
    assert [e["slug"] for e in result["status"]["experiences"]] == ["backend-intern-acme-corp", "hydra"]


def test_start_twice_is_refused(script):
    begin(script)
    with pytest.raises(interview.InterviewError):
        interview.start("")


def test_seed_text_is_used_once_and_discarded(script):
    script.replies.append("```json\n" + EXTRACTION + "\n```")
    interview.start("I also built a Discord bot")
    assert "Discord bot" in script.completes[0][1]
    assert interview.State.load().seed == "I also built a Discord bot"
    script.replies.append("EXPERIENCES: []\nSTART: yes\n---\nStarting.")
    script.replies.append("COVERED:\nDONE: no\n---\nFirst question?")
    events("go")
    assert interview.State.load().seed == ""


def test_setup_turn_starts_with_roles_first(script):
    _, evs = begin(script)
    assert "Good, let's start." in shown(evs)
    assert "Tell me the story" in shown(evs)
    assert any(e["type"] == "break" for e in evs)
    state = interview.State.load()
    assert state.phase == "interviewing" and state.current == "backend-intern-acme-corp"
    exp = interview.Experience.load("backend-intern-acme-corp")
    assert exp.asked == 1 and exp.transcript[0]["role"] == "assistant"
    # The first question is asked from the resume entry.
    assert "Built the billing service" in script.calls[1][1]["content"]


def test_setup_turn_can_add_an_experience(script):
    script.replies.append("```json\n" + EXTRACTION + "\n```")
    interview.start("")
    added = json.loads(EXTRACTION)["experiences"] + [
        {"title": "Discord bot", "kind": "project", "resume_entry": "a bot"}]
    script.replies.append(f"EXPERIENCES: {json.dumps(added)}\nSTART: no\n---\nAdded. Anything else?")
    evs = events("Add my Discord bot")
    assert interview.State.load().phase == "setup"
    assert [e["slug"] for e in interview.State.load().experiences][-1] == "discord-bot"
    assert shown(evs) == "Added. Anything else?"


def test_answer_marks_coverage_from_the_header_only_for_known_lines(script):
    begin(script)
    script.replies.append("COVERED: 1, 3, 4, 13, 99\nDONE: no\n---\nWhat was hardest?")
    evs = events("It was a Go service on Postgres, I built the ledger part myself.")
    exp = interview.Experience.load("backend-intern-acme-corp")
    # 13 is for experienced candidates only; 99 is not a line at all.
    assert sorted(exp.coverage) == ["1", "3", "4"]
    assert exp.asked == 2
    summary = [e for e in evs if e["type"] == "experience"][-1]
    assert summary["covered"] == 3 and summary["total"] == 12
    assert shown(evs) == "What was hardest?"


def test_reply_without_header_is_shown_whole_and_graded_separately(script):
    begin(script)
    script.replies.append("Roughly how many requests a day?")
    script.replies.append("COVERED: 1, 2\nDONE: no")  # the grading call
    evs = events("A lot of traffic. Summer 2025, team of four, for the billing team.")
    assert shown(evs) == "Roughly how many requests a day?"
    assert sorted(interview.Experience.load("backend-intern-acme-corp").coverage) == ["1", "2"]
    assert "## Answer" in script.completes[-1][1]


def test_done_closes_the_experience_writes_main_and_moves_on(script):
    begin(script)
    script.replies.append("COVERED: 2, 5, 6, 7, 8, 9, 10, 11, 12\nDONE: yes\n---\nThat covers it.")
    script.replies.append("# Backend Intern, Acme Corp\nKind: role\n\n## Context\n\nnot discussed\n")
    script.replies.append("COVERED:\nDONE: no\n---\nTell me about Hydra.")
    evs = events("Long answer covering everything.")
    exp = interview.Experience.load("backend-intern-acme-corp")
    assert exp.closed
    assert exp.coverage["1"] == "not_discussed" and exp.coverage["2"] == "covered"
    assert (profile.STORIES / "backend-intern-acme-corp" / "main.md").read_text().startswith("# Backend Intern")
    assert script.started == ["backend-intern-acme-corp"]
    # The transcript, not a summary, is what the document is written from.
    assert "Long answer covering everything." in script.completes[-1][1]
    assert interview.State.load().current == "hydra"
    assert "Tell me about Hydra." in shown(evs)


def test_ten_questions_then_the_wrapup_then_close(script):
    begin(script)
    exp = interview.Experience.load("backend-intern-acme-corp")
    exp.asked = 9
    exp.save()
    script.replies.append("COVERED: 7\nDONE: no\n---\nAnd what about the data model?")
    evs = events("About 2k requests a day.")
    exp = interview.Experience.load("backend-intern-acme-corp")
    assert exp.wrapup_asked and exp.asked == 10
    assert exp.transcript[-1]["content"] == "Anything about this one I missed?"
    assert any(e["type"] == "replace" and "missed" in e["text"] for e in evs)

    script.replies.append("COVERED: 9\nDONE: no\n---\nOne more question?")  # ignored: closing
    script.replies.append("# Backend Intern, Acme Corp\nKind: role\n")
    script.replies.append("COVERED:\nDONE: no\n---\nTell me about Hydra.")
    events("No, that is all.")
    exp = interview.Experience.load("backend-intern-acme-corp")
    assert exp.closed and exp.coverage["9"] == "covered"


def test_asking_to_move_on_closes_whatever_the_model_says(script):
    begin(script)
    # A move-on message carries no facts, so its COVERED line is ignored too.
    script.replies.append("COVERED: 1, 2, 3, 4, 5, 6, 7, 8, 9\nDONE: no\n---\nWhat was the architecture?")
    script.replies.append("# Backend Intern\nKind: role\n")
    script.replies.append("COVERED:\nDONE: no\n---\nTell me about Hydra.")
    events("Nothing more to add, move on.")
    exp = interview.Experience.load("backend-intern-acme-corp")
    assert exp.closed and exp.coverage["1"] == "not_discussed"
    assert interview.State.load().current == "hydra"


def test_last_experience_closes_into_the_open_phase(script):
    begin(script)
    hydra = interview.Experience(slug="hydra", title="Hydra", kind="project", closed=True)
    hydra.save()
    script.replies.append("COVERED:\nDONE: yes\n---\nDone.")
    script.replies.append("# Backend Intern\nKind: role\n")
    evs = events("Nothing more.")
    assert interview.State.load().phase == "open"
    assert "not written down anywhere" in shown(evs)


def test_open_phase_update_rewrites_main_and_regenerates(script):
    begin(script)
    state = interview.State.load()
    state.phase = "open"
    state.current = None
    state.save()
    folder = profile.STORIES / "backend-intern-acme-corp"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "main.md").write_text("# Backend Intern\nKind: role\n\n## Numbers\n\n30 percent faster\n")
    exp = interview.Experience.load("backend-intern-acme-corp")
    exp.closed = True
    exp.save()
    script.replies.append("ACTION: update\nSLUG: backend-intern-acme-corp\nTITLE:\nKIND:\n---\nUpdated the number.")
    script.replies.append("# Backend Intern\nKind: role\n\n## Numbers\n\n40 percent faster\n")
    evs = events("The number was 40 percent, not 30.")
    assert "40 percent" in (folder / "main.md").read_text()
    assert script.started == ["backend-intern-acme-corp"]
    assert shown(evs) == "Updated the number."


def test_open_phase_new_experience_starts_an_interview(script):
    begin(script)
    state = interview.State.load()
    state.phase = "open"
    state.current = None
    state.save()
    for slug in ("backend-intern-acme-corp", "hydra"):
        try:
            exp = interview.Experience.load(slug)
        except interview.InterviewError:
            exp = interview.Experience(slug=slug, title=slug, kind="project")
        exp.closed = True
        exp.save()
    script.replies.append("ACTION: new\nSLUG:\nTITLE: Discord bot\nKIND: project\n---\nLet's talk about it.")
    script.replies.append("COVERED:\nDONE: no\n---\nTell me the story of the Discord bot.")
    evs = events("I also wrote a Discord bot for my club.")
    state = interview.State.load()
    assert state.phase == "interviewing" and state.current == "discord-bot"
    assert "Discord bot" in shown(evs)


def test_open_phase_none_changes_nothing(script):
    begin(script)
    state = interview.State.load()
    state.phase = "open"
    state.save()
    script.replies.append("ACTION: none\nSLUG:\nTITLE:\nKIND:\n---\nHydra is a cache in Go.")
    evs = events("What did I say Hydra was?")
    assert shown(evs) == "Hydra is a cache in Go."
    assert script.started == []
    assert interview.State.load().phase == "open"


def test_llm_error_is_an_event_not_an_exception(script, monkeypatch):
    begin(script)

    def broken(*a, **k):
        raise interview.llm.LLMError("OpenRouter returned 500")
        yield  # pragma: no cover

    monkeypatch.setattr(interview.llm, "stream", broken)
    evs = events("hello")
    assert evs[-1] == {"type": "error", "message": "OpenRouter returned 500"}


def test_turn_before_start_is_an_error(script):
    evs = events("hello")
    assert evs[0]["type"] == "error" and "Start" in evs[0]["message"]


def test_children_and_index(script, monkeypatch):
    # Real generate_children with the stub model: tailor.md and star.md, then
    # an index line built from the tailor document's first lines.
    monkeypatch.setattr(interview.llm, "tailor_model", lambda: "heavy")
    folder = profile.STORIES / "hydra"
    folder.mkdir(parents=True)
    (folder / "main.md").write_text("# Hydra\nKind: project\n")
    interview.Experience(slug="hydra", title="Hydra", kind="project", closed=True).save()
    script.replies.append("```\nSummary: A distributed cache in Go for a class project.\nStack: Go, gRPC\n\n## Candidate bullets\n- x\n```")
    script.replies.append("## Situation\n...")
    interview.generate_children("hydra")
    assert (folder / "tailor.md").read_text().startswith("Summary:")  # fence stripped
    assert (folder / "star.md").exists()
    assert interview.Experience.load("hydra").children == "ready"
    assert profile.index() == "- [hydra] A distributed cache in Go for a class project. Stack: Go, gRPC"
    # Both prompts come from story_rules.md, each with only its own section.
    assert "## Tailor document" in script.completes[0][0] and "## Star document" not in script.completes[0][0]
    assert "## Star document" in script.completes[1][0]


def test_children_error_is_recorded(script, monkeypatch):
    folder = profile.STORIES / "hydra"
    folder.mkdir(parents=True)
    (folder / "main.md").write_text("# Hydra\n")
    interview.Experience(slug="hydra", title="Hydra", kind="project", closed=True).save()

    def broken(*a, **k):
        raise interview.llm.LLMError("boom")

    monkeypatch.setattr(interview.llm, "complete", broken)
    interview.generate_children("hydra")
    exp = interview.Experience.load("hydra")
    assert exp.children == "error" and "boom" in exp.children_error


def test_status_and_documents_over_http(script):
    client = TestClient(app)
    assert client.get("/profile").json()["phase"] == "new"
    begin(script)
    data = client.get("/profile").json()
    assert data["phase"] == "interviewing" and data["current"] == "backend-intern-acme-corp"
    assert data["transcript"][-1]["content"] == "Tell me the story of the billing service."
    assert client.get("/profile/experiences/hydra/main.md").status_code == 404
    assert client.get("/profile/experiences/hydra/state.json").status_code == 404
    assert client.get("/profile/experiences/../x/main.md").status_code in (404, 422)


def test_turn_streams_server_sent_events(script):
    client = TestClient(app)
    begin(script)
    script.replies.append("COVERED: 1\nDONE: no\n---\nWhat broke?")
    with client.stream("POST", "/profile/turn", json={"message": "Summer 2025, team of four."}) as response:
        assert response.headers["content-type"].startswith("text/event-stream")
        body = "".join(response.iter_text())
    evs = [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]
    assert "".join(e["text"] for e in evs if e["type"] == "delta") == "What broke?"
    assert evs[-1] == {"type": "done"}


def test_regenerate_needs_a_closed_experience(script):
    client = TestClient(app)
    begin(script)
    assert client.post("/profile/experiences/backend-intern-acme-corp/regenerate").status_code == 409
    assert client.post("/profile/experiences/nope/regenerate").status_code == 404


def test_glm_models_get_low_effort_not_disabled_reasoning():
    from tailor import llm
    assert llm.minimal_reasoning("z-ai/glm-5.3-flash") == {"effort": "low"}
    assert llm.minimal_reasoning("deepseek/deepseek-v4-flash") == {"enabled": False}


def test_seed_file_extracts_pdf_text_and_rejects_scans(tmp_path, monkeypatch):
    from pypdf import PdfWriter
    client = TestClient(app)
    # Text files pass through; a PDF with a text layer is read by pypdf.
    r = client.post("/profile/seed-file", content=b"# Notes\nBuilt a bot.",
                    headers={"X-Filename": "notes.md"})
    assert r.json() == {"name": "notes.md", "text": "# Notes\nBuilt a bot."}
    blank = PdfWriter()
    blank.add_blank_page(width=200, height=200)
    path = tmp_path / "scan.pdf"
    with open(path, "wb") as f:
        blank.write(f)
    r = client.post("/profile/seed-file", content=path.read_bytes(), headers={"X-Filename": "scan.pdf"})
    assert r.status_code == 422 and "no text layer" in r.json()["detail"]
    r = client.post("/profile/seed-file", content=b"%PDF-1.4 garbage", headers={"X-Filename": "bad.pdf"})
    assert r.status_code == 422 and "could not read" in r.json()["detail"]


def test_reply_parts_are_labelled_stripped_and_flagged_for_speech(script):
    begin(script)
    script.replies.append("COVERED: 1\nDONE: no\n---\nack: Got it.\nnote: Ledger in Go on Postgres.\nask: What was hardest?")
    evs = events("A Go service on Postgres, team of four.")
    deltas = [e for e in evs if e["type"] == "delta"]
    assert "".join(e["text"] for e in deltas) == "Got it.\nLedger in Go on Postgres.\nWhat was hardest?"
    assert {e["part"] for e in deltas} == {"ack", "note", "ask"}
    assert all(e["spoken"] == (e["part"] != "note") for e in deltas)
    # The transcript keeps the words, not the labels.
    exp = interview.Experience.load("backend-intern-acme-corp")
    assert exp.transcript[-1]["content"] == "Got it.\nLedger in Go on Postgres.\nWhat was hardest?"


def test_reply_without_labels_carries_no_part_so_the_page_decides():
    parts = interview._Parts()
    out = parts.feed("Roughly how many? ") + parts.feed("And when?\nask") + parts.flush()
    assert out == [("Roughly how many? ", None), ("And when?\n", None), ("ask", None)]
    # A label split across chunks still lands, and a false start is text.
    parts = interview._Parts()
    assert parts.feed("as") == []
    assert parts.feed("k: Why?\nno") == [("Why?\n", "ask")]
    assert parts.feed("t a label") == [("not a label", "ask")]
