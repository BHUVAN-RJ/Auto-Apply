"""Use Opus by hand: the prompt that goes out, and the LaTeX that comes back.

The point of the feature is that nothing about the rules changes when the
answer arrives through a chat window instead of an API. These tests are
mostly about that: the prompt is the API's own prompt, and the reply meets
the API's own checkers.
"""

from __future__ import annotations

import pytest

from tailor import byhand, tailor as tailor_module
from tailor.fetch import Posting

MASTER = r"""\documentclass{article}
\begin{document}
\section{EXPERIENCE}
\resumeItem{Built a service that cut latency by 40\% using Python and Redis.}
\resumeItem{Led a rewrite of the billing path, removing 12k lines.}
\end{document}
"""

POSTING = Posting(url="https://example.com/jobs/1", text="We want Python and Redis.",
                  title="Backend Engineer", company="Example")


def test_the_prompt_is_the_api_s_own_prompt(monkeypatch):
    """Not a second copy of the rules. What a person pastes into a chat is
    what the model would have been sent, or the two drift and the checkers
    start judging answers to a question nobody asked."""
    hand = byhand.build(POSTING, MASTER, profile="Some profile")
    assert tailor_module.system_prompt() in hand.prompt
    assert tailor_module._build_user_message(POSTING, MASTER, "Some profile") in hand.prompt
    assert MASTER.strip()[:40] in hand.prompt
    assert hand.characters == len(hand.prompt)


def test_the_reviewer_s_own_words_travel_with_it():
    hand = byhand.build(POSTING, MASTER, profile="p", extra_instruction="shorter, please")
    assert "shorter, please" in hand.prompt
    assert "The reviewer asked for a change" in hand.prompt


def test_by_hand_is_not_a_model_name():
    """Nothing may ever send it to a provider."""
    assert byhand.is_by_hand(byhand.BY_HAND)
    assert byhand.is_by_hand("By-Hand")
    assert not byhand.is_by_hand("anthropic/claude-opus-5.5")
    assert not byhand.is_by_hand(None)
    assert "/" not in byhand.BY_HAND


# -- what comes back ------------------------------------------------------

def tailored(body: str) -> str:
    return MASTER.replace(
        r"\resumeItem{Built a service that cut latency by 40\% using Python and Redis.}", body)


def test_a_fenced_reply_and_a_bare_document_are_both_accepted():
    good = tailored(r"\resumeItem{Cut latency 40\% in Python and Redis, serving 9k requests.}")
    assert byhand.extract(f"Here you go:\n\n```tex\n{good}\n```\n") == good.strip()
    assert byhand.extract(good) == good.strip()


def test_a_chat_turn_that_is_not_a_resume_is_refused():
    for reply in ("Sure! Happy to help with that.", "", "```tex\n```", "I cannot do that."):
        assert byhand.extract(reply) is None


def test_the_pasted_resume_goes_through_the_same_checkers(monkeypatch):
    """An invented figure is refused here exactly as it is on the API path:
    the person went to a chat window, not around the rules."""
    monkeypatch.setattr(tailor_module, "under_tailored", lambda *a: None)
    monkeypatch.setattr(tailor_module, "unused_swap", lambda *a: None)

    invented = tailored(r"\resumeItem{Cut latency by 91\% for 4.2 million users.}")
    with pytest.raises(tailor_module.TailorError):
        byhand.accept(f"```tex\n{invented}\n```", POSTING, MASTER, profile="")


def test_a_good_paste_comes_back_as_an_ordinary_result(monkeypatch):
    monkeypatch.setattr(tailor_module, "under_tailored", lambda *a: None)
    monkeypatch.setattr(tailor_module, "unused_swap", lambda *a: None)

    good = tailored(r"\resumeItem{Cut latency 40\% in Python and Redis.}")
    result = byhand.accept(f"```tex\n{good}\n```\n```rationale\nswapped a word\n```",
                           POSTING, MASTER, profile="")
    assert result.tex == good.strip()
    assert "swapped a word" in result.suggestions
    assert byhand.BY_HAND in result.model
    assert result.diff


def test_a_reply_with_no_rationale_still_lands(monkeypatch):
    monkeypatch.setattr(tailor_module, "under_tailored", lambda *a: None)
    monkeypatch.setattr(tailor_module, "unused_swap", lambda *a: None)

    good = tailored(r"\resumeItem{Cut latency 40\% in Python and Redis.}")
    result = byhand.accept(good, POSTING, MASTER, profile="")
    assert "no rationale" in result.suggestions


def test_the_soft_floors_are_warnings_not_refusals(monkeypatch):
    """On the API path the volume floor is a rejection only while attempts
    remain. A person who has been to a chat window and back has spent more
    than an attempt, so it is said and the resume is kept."""
    monkeypatch.setattr(tailor_module, "under_tailored", lambda *a: "only 1 of 2 bullets changed")
    monkeypatch.setattr(tailor_module, "unused_swap", lambda *a: None)

    good = tailored(r"\resumeItem{Cut latency 40\% in Python and Redis.}")
    result = byhand.accept(good, POSTING, MASTER, profile="")
    assert "only 1 of 2 bullets changed" in result.warnings


def test_the_posting_comes_back_off_disk_for_the_checkers():
    """The handoff returns hours later and re-fetching would be a different
    posting; the one the prompt was built from is what the answer is judged
    against."""
    saved = POSTING.to_markdown()
    back = Posting.from_markdown(saved, url=POSTING.url)
    assert back.text.strip() == POSTING.text.strip()
    assert back.title == POSTING.title and back.company == POSTING.company
