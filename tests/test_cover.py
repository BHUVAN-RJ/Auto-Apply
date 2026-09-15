"""The cover letter step: shape checks, LaTeX safety, and the retry."""

import datetime as dt

import pytest

from tailor import cover
from tailor.fetch import Posting

POSTING = Posting(url="https://example.com/j/1", text="Build APIs.", title="Backend Engineer",
                  company="Example Corp")
GOOD = ("Dear Hiring Manager,\n\n" + ("I built things. " * 40).strip()
        + "\n\nSincerely,\nJane Doe")
DASHED = GOOD.replace("I built things. I built things.", "I built things — and more. I built things.", 1)


def test_clean_strips_a_code_fence_and_extra_blank_lines():
    assert cover.clean("```text\nDear X,\n\n\n\nBody\n```") == "Dear X,\n\nBody"
    assert cover.clean("  plain  ") == "plain"


def test_escape_handles_every_latex_special():
    out = cover.escape(r"50% & $1M # a_b {x} ~ ^ \ ")
    assert r"\%" in out and r"\&" in out and r"\$" in out and r"\#" in out
    assert r"\_" in out and r"\{" in out and r"\}" in out
    assert r"\textasciitilde{}" in out and r"\textasciicircum{}" in out
    assert r"\textbackslash{}" in out
    assert out.count("\\textbackslash") == 1, "escaping must not re-escape its own output"


def test_to_tex_keeps_the_sign_off_on_adjacent_lines():
    tex = cover.to_tex("Dear X,\n\nBody 100%.\n\nSincerely,\nJane Doe", today=dt.date(2026, 9, 11))
    assert "September 11, 2026" in tex
    assert "Body 100\\%." in tex
    assert "Sincerely, \\\\\nJane Doe" in tex
    assert tex.count("\\documentclass") == 1
    assert "fontspec" not in tex, "pdflatex only; BasicTeX has no fonts to spec"


def test_write_accepts_a_well_shaped_letter(monkeypatch):
    calls = []

    def fake_complete(system, user, **kwargs):
        calls.append((system, user))
        return GOOD

    monkeypatch.setattr(cover.llm, "complete", fake_complete)
    monkeypatch.setattr(cover.llm, "tailor_model", lambda: "test/model")
    letter = cover.write(POSTING, "\\documentclass{article}", profile="Likes Go", extra_instruction="mention Go")

    assert letter.text == GOOD and letter.attempts == 1 and letter.model == "test/model"
    system, user = calls[0]
    assert "Never invent" in system or "never invent" in system.lower()
    assert "Example Corp" in user and "Likes Go" in user and "mention Go" in user
    assert "visa" in system.lower(), "the rules must forbid visa talk in the letter"


def test_dashes_are_recognised_but_hyphenated_words_are_not():
    assert cover.has_dash("the role — which")
    assert cover.has_dash("the role – which")
    assert cover.has_dash("the role - which")
    assert cover.has_dash("the role -- which")
    assert not cover.has_dash("end-to-end, high-throughput work")


def test_strip_dashes_leaves_clean_punctuation():
    out = cover.strip_dashes("the role — which I like — is good.\nAlso this - and that -- end —\nDone —")
    assert out == "the role, which I like, is good.\nAlso this, and that, end\nDone"
    assert ", ," not in out and " ," not in out


def test_a_dashed_letter_is_sent_back(monkeypatch):
    replies = iter([DASHED, GOOD])
    seen = []

    def fake_complete(system, user, **kwargs):
        seen.append(user)
        return next(replies)

    monkeypatch.setattr(cover.llm, "complete", fake_complete)
    monkeypatch.setattr(cover.llm, "tailor_model", lambda: "test/model")
    letter = cover.write(POSTING, "x")
    assert letter.text == GOOD and letter.attempts == 2
    assert "used a dash" in seen[1]


def test_dashes_that_survive_every_retry_are_rewritten_not_shipped(monkeypatch):
    monkeypatch.setattr(cover.llm, "complete", lambda *a, **k: DASHED)
    monkeypatch.setattr(cover.llm, "tailor_model", lambda: "test/model")
    letter = cover.write(POSTING, "x")
    assert "—" not in letter.text and "things, and more" in letter.text
    assert letter.attempts == cover.ATTEMPTS


def test_the_rules_forbid_dashes_and_cliches():
    rules = cover.RULES.read_text()
    assert "em dash" in rules and "passionate about" in rules


def test_write_retries_once_when_the_shape_is_wrong(monkeypatch):
    replies = iter(["Too short.", GOOD])
    seen = []

    def fake_complete(system, user, **kwargs):
        seen.append(user)
        return next(replies)

    monkeypatch.setattr(cover.llm, "complete", fake_complete)
    monkeypatch.setattr(cover.llm, "tailor_model", lambda: "test/model")
    letter = cover.write(POSTING, "x")
    assert letter.attempts == 2
    assert "previous reply was rejected" in seen[1] and "2 words" in seen[1]
    assert "Sincerely" in seen[1]


def test_write_gives_up_after_the_retry(monkeypatch):
    monkeypatch.setattr(cover.llm, "complete", lambda *a, **k: "Nope.")
    monkeypatch.setattr(cover.llm, "tailor_model", lambda: "test/model")
    with pytest.raises(cover.CoverLetterError, match=f"{cover.ATTEMPTS} attempts"):
        cover.write(POSTING, "x")


def test_a_letter_without_a_sign_off_is_rejected(monkeypatch):
    replies = iter([("word " * 200).strip(), GOOD])
    monkeypatch.setattr(cover.llm, "complete", lambda *a, **k: next(replies))
    monkeypatch.setattr(cover.llm, "tailor_model", lambda: "test/model")
    assert cover.write(POSTING, "x").attempts == 2
