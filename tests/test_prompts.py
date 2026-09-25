import json

import pytest
from fastapi.testclient import TestClient

from server import prompts as prompts_api
from server import thread
from server.app import app
from tailor import prompts, tailor, workshop


def test_every_prompt_has_stock_text():
    for prompt in prompts.CATALOG:
        assert prompt.stock().strip(), prompt.name


def test_text_is_stock_until_edited_then_the_edit():
    stock = prompts.stock("cover")
    assert prompts.text("cover") == stock
    prompts.save("cover", stock + "\nNever use the word passionate.\n")
    assert prompts.text("cover").endswith("Never use the word passionate.\n")
    assert prompts.detail("cover")["edited"] is True


def test_system_prompt_reads_the_edit():
    prompts.save("tailor.format", "SHAPE ONLY")
    assert tailor.system_prompt().endswith("SHAPE ONLY")


def test_saving_the_stock_text_is_a_reset_and_history_keeps_the_edit():
    stock = prompts.stock("answers")
    prompts.save("answers", stock + "\nmine\n")
    prompts.save("answers", stock)
    assert prompts.detail("answers")["edited"] is False
    versions = prompts.history("answers")
    assert len(versions) == 2
    prompts.restore("answers", versions[0]["version"])
    assert prompts.text("answers").endswith("mine\n")


def test_reset_keeps_the_edit_in_history():
    prompts.save("screen", "short rules")
    prompts.reset("screen")
    assert prompts.text("screen") == prompts.stock("screen")
    assert prompts.history("screen")[0]["why"] == "reset"


def test_unknown_prompt():
    with pytest.raises(prompts.PromptError):
        prompts.text("nope")


def _stock_is(monkeypatch, name, value):
    original = prompts.BY_NAME[name]
    monkeypatch.setitem(prompts.BY_NAME, name,
                        prompts.Prompt(name, original.title, original.group, original.note,
                                       lambda: value))
    monkeypatch.setattr(prompts, "CATALOG", tuple(prompts.BY_NAME.values()))


def test_sync_merges_an_update_onto_the_persons_edit(monkeypatch):
    _stock_is(monkeypatch, "cover", "one\ntwo\nthree\nfour\nfive\n")
    prompts.save("cover", "one\ntwo, mine\nthree\nfour\nfive\n")
    _stock_is(monkeypatch, "cover", "one\ntwo\nthree\nfour\nfive, updated\n")
    report = prompts.sync()
    assert report == [{"name": "cover", "merged": True}]
    assert prompts.text("cover") == "one\ntwo, mine\nthree\nfour\nfive, updated\n"
    # The base moved with it: a second sync has nothing to do.
    assert prompts.sync() == []


def test_sync_conflict_keeps_the_persons_edit_working(monkeypatch):
    _stock_is(monkeypatch, "cover", "a\nb\nc\n")
    prompts.save("cover", "a\nb mine\nc\n")
    _stock_is(monkeypatch, "cover", "a\nb theirs\nc\n")
    assert prompts.sync() == [{"name": "cover", "merged": False}]
    assert prompts.text("cover") == "a\nb mine\nc\n"
    detail = prompts.detail("cover")
    assert detail["conflict"] and "b theirs" in detail["conflict_text"]
    # Saving is the resolution, made against today's stock.
    prompts.save("cover", "a\nb both\nc\n")
    assert prompts.detail("cover")["conflict"] is False
    assert prompts.sync() == []


# ---------------------------------------------------------- workshop --


REPLY = """Added a rule against "passionate" to the cover letter.

<<<<<<< SEARCH cover
{search}
=======
{search}
Never call the applicant passionate.
>>>>>>> REPLACE
"""


def _first_line(name):
    return next(line for line in prompts.text(name).splitlines() if line.strip())


def test_parse_and_apply_edits():
    line = _first_line("cover")
    message, edits = workshop.parse(REPLY.format(search=line))
    assert message.startswith("Added a rule")
    texts, problems = workshop.apply_edits(edits)
    assert not problems
    assert "Never call the applicant passionate." in texts["cover"]


def test_edit_that_does_not_match_is_a_problem():
    _, edits = workshop.parse(REPLY.format(search="text that is not there"))
    texts, problems = workshop.apply_edits(edits)
    assert not texts and "does not occur" in problems[0]


def test_empty_search_appends():
    _, edits = workshop.parse("ok\n<<<<<<< SEARCH answers\n=======\nLast rule.\n>>>>>>> REPLACE\n")
    texts, _ = workshop.apply_edits(edits)
    assert texts["answers"].endswith("Last rule.\n")


def _stub_models(monkeypatch, replies, picked="cover"):
    sent = []
    monkeypatch.setattr(workshop.llm, "complete", lambda *a, **k: picked)

    def chat(messages, **kwargs):
        sent.append(messages)
        return replies.pop(0)
    monkeypatch.setattr(workshop.llm, "chat", chat)
    return sent


def test_propose_then_apply(monkeypatch):
    line = _first_line("cover")
    sent = _stub_models(monkeypatch, [REPLY.format(search=line)])
    proposal = workshop.propose("stop calling me passionate")
    assert proposal["state"] == "pending" and proposal["prompts"] == ["cover"]
    assert "+Never call the applicant passionate." in proposal["diffs"]["cover"]
    # The picked prompt went in full; nothing is applied yet.
    assert prompts.stock("cover") in sent[0][1]["content"]
    assert prompts.detail("cover")["edited"] is False
    workshop.apply(proposal["id"])
    assert "passionate" in prompts.text("cover")
    assert prompts.history("cover")[0]["why"] == "workshop"


def test_propose_retries_once_then_fails(monkeypatch):
    bad = REPLY.format(search="missing")
    _stub_models(monkeypatch, [bad, bad])
    proposal = workshop.propose("x")
    assert proposal["state"] == "failed" and "does not occur" in proposal["error"]


def test_apply_is_refused_when_the_prompt_moved(monkeypatch):
    line = _first_line("cover")
    _stub_models(monkeypatch, [REPLY.format(search=line)])
    proposal = workshop.propose("x")
    prompts.save("cover", "rewritten by hand\n")
    assert workshop.apply(proposal["id"])["state"] == "stale"


def test_learn_reads_change_requests_across_jobs(tmp_path, monkeypatch):
    monkeypatch.setattr(thread, "THREADS", tmp_path / "threads")
    for job in ("a1", "b2"):
        thread.add(job, "change", "the summary is too long")
    thread.add("b2", "change", "Re-tailor this on the stronger model. No new instruction.")
    sent = _stub_models(monkeypatch, ["Nothing standing.\n"])
    proposal = workshop.learn()
    assert proposal["source"] == "learned" and proposal["state"] == "empty"
    assert len(proposal["evidence"]) == 2
    assert sent[0][1]["content"].count("the summary is too long") == 2
    assert "stronger model" not in sent[0][1]["content"]
    # Learned once: nothing new, no second call.
    assert workshop.learn() is None


# ------------------------------------------------------------- api --


def test_prompt_endpoints():
    client = TestClient(app)
    names = [p["name"] for p in client.get("/prompts").json()["prompts"]]
    assert "tailor" in names and "workshop" in names
    saved = client.post("/prompts/cover", json={"text": "short\n"}).json()
    assert saved["edited"] and saved["text"] == "short\n"
    assert client.post("/prompts/cover", json={"text": "  "}).status_code == 400
    assert client.post("/prompts/cover/reset").json()["edited"] is False
    assert client.get("/prompts/nope").status_code == 404


def test_key_is_checked_then_written(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("# OPENROUTER_API_KEY=\nOTHER=1\n")
    monkeypatch.setattr(prompts_api.paths, "ENV_FILE", env)
    monkeypatch.setattr(prompts_api, "check_key", lambda key: None)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    client = TestClient(app)
    assert client.post("/setup/key", json={"key": "hello"}).status_code == 400
    key = "sk-or-v1-" + "a" * 40
    assert client.post("/setup/key", json={"key": key}).json()["key"] is True
    assert env.read_text() == f"OPENROUTER_API_KEY={key}\nOTHER=1\n"
    monkeypatch.delenv("OPENROUTER_API_KEY")


def test_onboarding_until_done_or_a_job_exists(tmp_path, monkeypatch):
    from server import queue, settings
    monkeypatch.setattr(settings, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(queue, "all_jobs", lambda: [])
    client = TestClient(app)
    assert client.get("/setup").json()["onboarded"] is False
    assert client.post("/setup/done").json()["onboarded"] is True
    settings.save(onboarded=False)
    monkeypatch.setattr(queue, "all_jobs", lambda: ["a job"])
    assert client.get("/setup").json()["onboarded"] is True


def test_setup_opens_only_named_pages(monkeypatch):
    opened = []

    class Reply:
        def read(self):
            return b"{}"
    monkeypatch.setattr(prompts_api.urllib.request, "urlopen",
                        lambda request, timeout: opened.append(request.full_url) or Reply())
    client = TestClient(app)
    assert client.post("/setup/open", json={"page": "https://evil.example"}).status_code == 404
    assert client.post("/setup/open", json={"page": "jobright"}).json()["opened"] is True
    assert opened[0].endswith("/json/new?https://jobright.ai/")


def test_the_resume_is_uploaded_as_latex_only(tmp_path, monkeypatch):
    base = tmp_path / "base"
    monkeypatch.setattr(prompts_api.paths, "BASE", base)
    compiled = []
    monkeypatch.setattr(prompts_api.texc, "compile_pdf", lambda tex, out: compiled.append(tex))
    client = TestClient(app)
    assert client.post("/setup/resume", json={"name": "cv.pdf", "text": "%PDF"}).status_code == 400
    assert client.post("/setup/resume", json={"name": "cv.tex", "text": "hello"}).status_code == 400
    template = prompts_api.TEMPLATE.read_text()
    reply = client.post("/setup/resume", json={"name": "cv.tex", "text": template}).json()
    assert reply["resume"] is True and reply["compiled"] is True and compiled
    other = "\\documentclass{article}\\begin{document}\\section{Experience}x\\end{document}"
    reply = client.post("/setup/resume", json={"name": "mine.tex", "text": other}).json()
    assert reply["resume"] is False and reply["resume_problems"]
    # The one it replaced is kept.
    assert len(list(base.glob("resume.*.tex"))) == 1
    assert client.get("/setup/template").text == template
