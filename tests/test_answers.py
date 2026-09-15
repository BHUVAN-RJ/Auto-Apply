"""Free-form answers: written by the tailor model, never for visa questions."""

import pytest

from browser import guard
from tailor import answers

GOOD = ("I want a team where I own a service end to end and ship often. At Stylebot I ran "
        "an NLP microservice for 20 clients and liked being on the hook for it. I would "
        "avoid a role that is only maintenance with no product work.")
CONTEXT = answers.Context(posting="Confido builds AI for CPG.", resume_tex="\\documentclass{article}",
                         profile="Likes ownership", cover_letter="Dear X,\n\nHi.\n\nSincerely,\nJane")


def stub(monkeypatch, *replies):
    seen = []
    it = iter(replies)

    def fake(system, user, **kwargs):
        seen.append((system, user))
        return next(it)

    monkeypatch.setattr(answers.llm, "complete", fake)
    monkeypatch.setattr(answers.llm, "tailor_model", lambda: "test/model")
    return seen


def test_answer_uses_the_tailor_model_with_the_full_context(monkeypatch):
    seen = stub(monkeypatch, GOOD)
    result = answers.answer("What are you looking for in your next role?", CONTEXT)
    assert result.text == GOOD and result.model == "test/model" and result.attempts == 1
    system, user = seen[0]
    for piece in ("Confido builds", "Likes ownership", "documentclass", "Dear X", "next role?"):
        assert piece in user
    assert "em dash" in system and "passionate about" in system


def test_visa_questions_are_refused_before_any_model_call(monkeypatch):
    seen = stub(monkeypatch, GOOD)
    with pytest.raises(guard.ProtectedField):
        answers.answer("Will you now or in the future require visa sponsorship?", CONTEXT)
    assert seen == []


def test_a_dashed_or_long_answer_is_sent_back(monkeypatch):
    dashed = GOOD.replace("ship often.", "ship often — and learn.")
    seen = stub(monkeypatch, dashed, GOOD)
    assert answers.answer("Why here?", CONTEXT).attempts == 2
    assert "used a dash" in seen[1][1]


def test_dashes_surviving_the_retry_are_rewritten(monkeypatch):
    dashed = GOOD.replace("ship often.", "ship often — and learn.")
    stub(monkeypatch, dashed, dashed)
    text = answers.answer("Why here?", CONTEXT).text
    assert "—" not in text and "often, and learn" in text


def test_markdown_and_wrapping_quotes_are_rejected_or_stripped(monkeypatch):
    stub(monkeypatch, f'"{GOOD}"')
    assert answers.answer("Why?", CONTEXT).text == GOOD
    assert answers.problems("- one\n- two " + "word " * 20)
    assert answers.problems("**bold** " + "word " * 20)


def test_the_answerer_remembers_and_dedupes(monkeypatch):
    seen = stub(monkeypatch, GOOD)
    ask = answers.Answerer(CONTEXT)
    assert ask("Why  here?") == GOOD
    assert ask("Why here?") == GOOD, "the same question is not sent twice"
    assert len(seen) == 1
    md = ask.to_markdown()
    assert md.startswith("# Free-form answers") and "**Why here?**" in md and GOOD in md
    assert answers.Answerer(CONTEXT).to_markdown() == ""


def test_context_reads_the_application_folder(tmp_path):
    (tmp_path / "posting.md").write_text("# Role")
    (tmp_path / "resume.tex").write_text("tex")
    ctx = answers.Context.from_app_dir(tmp_path, profile="p", applicant="a")
    assert ctx.posting == "# Role" and ctx.resume_tex == "tex" and ctx.cover_letter == ""
    assert "## Candidate profile" in ctx.as_message() and "cover letter" not in ctx.as_message()
    assert answers.Context.from_app_dir(None).as_message() == ""
