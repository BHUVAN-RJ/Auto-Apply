"""Tailoring: block extraction, the frozen-section and length gates, retries."""

import pytest

from tailor import tailor
from tailor.fetch import Posting

# A miniature of the real template: same section heading form, same
# \resumeItem{\normalsize{...}} bullet shape, so the parsers are exercised
# against the structure they actually meet.
ORIGINAL = r"""\documentclass{article}
\newcommand{\resumeItem}[1]{\item{#1}}
\begin{document}
\section{\texorpdfstring{\color{airforceblue}SUMMARY}{}}
{\normalsize Software engineer with backend and ML experience.}
\section{\texorpdfstring{\color{airforceblue}EDUCATION}{}}
MS Computer Science, 2026.
\section{\texorpdfstring{\color{airforceblue}EXPERIENCE}{}}
\resumeItem{\normalsize{Built a scalable Python service handling 50K requests per day}}
\resumeItem{\normalsize{Cut p95 latency 52 percent with a Redis caching layer}}
\section{\texorpdfstring{\color{airforceblue}ACHIEVEMENTS}{}}
Early Career Innovator Award, 2025.
\end{document}
"""

POSTING = Posting(
    url="https://example.com/jobs/1",
    text="Backend role. Distributed systems, caching, Python. " * 12,
    title="Backend Engineer",
    company="Example Corp",
)


def reply(tex: str, verdict: str = "MATCH: backend and caching experience lines up.",
          rationale: str = "- reworded a bullet") -> str:
    return (
        f"```verdict\n{verdict}\n```\n\n"
        f"```tex\n{tex}```\n\n"
        f"```markdown\n{rationale}\n```\n"
    )


def stub_model(monkeypatch, *replies):
    """Serve `replies` in order, and record every prompt sent."""
    sent = []

    def complete(system, user, **kwargs):
        sent.append(user)
        return replies[min(len(sent) - 1, len(replies) - 1)]

    monkeypatch.setattr(tailor.llm, "complete", complete)
    monkeypatch.setattr(tailor.llm, "tailor_model", lambda: "test/model")
    return sent


# --- parsing -----------------------------------------------------------------

def test_split_sections_finds_every_heading():
    sections = tailor.split_sections(ORIGINAL)
    assert set(sections) == {"PREAMBLE", "SUMMARY", "EDUCATION", "EXPERIENCE", "ACHIEVEMENTS"}
    assert "Redis" in sections["EXPERIENCE"]


def test_bullet_lengths_reads_every_resume_item():
    assert len(tailor.bullet_lengths(ORIGINAL)) == 2


def test_extract_block_reads_each_fence():
    text = reply(ORIGINAL)
    assert tailor._extract_block(text, ("tex", "latex")).startswith(r"\documentclass")
    assert tailor._extract_block(text, ("verdict",)).startswith("MATCH")
    assert tailor._extract_block(text, ("markdown", "md")) == "- reworded a bullet"


def test_make_diff_is_a_unified_diff():
    after = ORIGINAL.replace("scalable Python service", "scalable distributed service")
    diff = tailor.make_diff(ORIGINAL, after)
    assert "+" in diff and "distributed" in diff
    assert diff.startswith("--- a/resume.tex")


# --- the gates ---------------------------------------------------------------

def test_validate_accepts_an_edit_inside_an_editable_section():
    after = ORIGINAL.replace("scalable Python service", "scalable Python backend")
    assert tailor._validate(ORIGINAL, after) == []


def test_validate_rejects_an_edit_to_a_frozen_section():
    after = ORIGINAL.replace("Early Career Innovator Award, 2025.", "Some Other Award, 2025.")
    with pytest.raises(tailor.TailorError, match="modified ACHIEVEMENTS"):
        tailor._validate(ORIGINAL, after)


def test_validate_rejects_an_edit_to_education():
    after = ORIGINAL.replace("MS Computer Science, 2026.", "MS Computer Science, 2025.")
    with pytest.raises(tailor.TailorError, match="modified EDUCATION"):
        tailor._validate(ORIGINAL, after)


def test_validate_rejects_a_dropped_section():
    after = ORIGINAL.replace(
        r"\section{\texorpdfstring{\color{airforceblue}ACHIEVEMENTS}{}}" "\n"
        "Early Career Innovator Award, 2025.\n", "")
    with pytest.raises(tailor.TailorError, match="dropped section"):
        tailor._validate(ORIGINAL, after)


def test_validate_rejects_a_changed_bullet_count():
    after = ORIGINAL.replace(
        r"\resumeItem{\normalsize{Cut p95 latency 52 percent with a Redis caching layer}}", "")
    with pytest.raises(tailor.TailorError, match="bullet count changed"):
        tailor._validate(ORIGINAL, after)


def test_validate_warns_when_a_bullet_grows_a_lot():
    after = ORIGINAL.replace(
        "Cut p95 latency 52 percent with a Redis caching layer",
        "Cut p95 latency 52 percent with a Redis caching layer " + "and more words " * 6)
    warnings = tailor._validate(ORIGINAL, after)
    assert len(warnings) == 1 and "bullet 2" in warnings[0]


def test_validate_ignores_pure_whitespace_reflow():
    after = ORIGINAL.replace("MS Computer Science, 2026.", "MS Computer Science,\n2026.")
    assert tailor._validate(ORIGINAL, after) == []


def test_validate_rejects_unbalanced_braces():
    with pytest.raises(tailor.TailorError, match="unbalanced braces"):
        tailor._validate(ORIGINAL, ORIGINAL + "\\textbf{oops")


def test_validate_rejects_empty_output():
    with pytest.raises(tailor.TailorError, match="empty"):
        tailor._validate(ORIGINAL, "   \n  ")


# --- the loop ----------------------------------------------------------------

def test_happy_path(monkeypatch):
    after = ORIGINAL.replace("scalable Python service", "scalable Python backend")
    stub_model(monkeypatch, reply(after))
    result = tailor.tailor(POSTING, resume_tex=ORIGINAL)
    assert "Python backend" in result.tex
    assert result.attempts == 1
    assert result.verdict.startswith("MATCH")
    assert result.model == "test/model"


def test_mismatch_raises_and_tailors_nothing(monkeypatch):
    stub_model(monkeypatch, "```verdict\nMISMATCH: role requires 10 years and a clearance.\n```")
    with pytest.raises(tailor.Mismatch, match="clearance"):
        tailor.tailor(POSTING, resume_tex=ORIGINAL)


def test_retries_when_a_frozen_section_was_touched(monkeypatch):
    bad = ORIGINAL.replace("Early Career Innovator Award, 2025.", "Fake Award, 2025.")
    good = ORIGINAL.replace("scalable Python service", "scalable Python backend")
    sent = stub_model(monkeypatch, reply(bad), reply(good))

    result = tailor.tailor(POSTING, resume_tex=ORIGINAL)

    assert result.attempts == 2
    assert "Fake Award" not in result.tex
    assert "modified ACHIEVEMENTS" in sent[1], "the model must be told what it broke"


def test_retries_until_the_page_count_fits(monkeypatch):
    long = ORIGINAL.replace("Cut p95", "Cut, at some considerable length, p95")
    short = ORIGINAL.replace("scalable Python service", "scalable Python backend")
    sent = stub_model(monkeypatch, reply(long), reply(short))
    pages = {"n": 0}

    def page_check(tex):
        pages["n"] += 1
        return 2 if pages["n"] == 1 else 1

    result = tailor.tailor(POSTING, resume_tex=ORIGINAL, page_check=page_check, target_pages=1)

    assert result.attempts == 2
    assert "compiled to 2 pages" in sent[1]
    assert "Do not delete a bullet" in sent[1]


def test_gives_up_after_max_attempts(monkeypatch):
    bad = ORIGINAL.replace("Early Career Innovator Award, 2025.", "Fake Award, 2025.")
    stub_model(monkeypatch, reply(bad))
    with pytest.raises(tailor.TailorError, match="gave up after 3 attempts"):
        tailor.tailor(POSTING, resume_tex=ORIGINAL)


def test_uncompilable_candidate_does_not_block_the_result(monkeypatch):
    """An unknown page count is accepted; the real compile reports the error."""
    after = ORIGINAL.replace("scalable Python service", "scalable Python backend")
    stub_model(monkeypatch, reply(after))
    result = tailor.tailor(POSTING, resume_tex=ORIGINAL, page_check=lambda tex: None)
    assert result.attempts == 1


def test_prompt_carries_the_bullet_length_budget(monkeypatch):
    after = ORIGINAL.replace("scalable Python service", "scalable Python backend")
    sent = stub_model(monkeypatch, reply(after))
    tailor.tailor(POSTING, resume_tex=ORIGINAL)
    assert "bullet 1:" in sent[0] and "characters" in sent[0]


def test_no_tex_block_is_retried_then_fails(monkeypatch):
    stub_model(monkeypatch, "```verdict\nMATCH: fine\n```\n\nI could not do it.")
    with pytest.raises(tailor.TailorError, match="no ```tex block"):
        tailor.tailor(POSTING, resume_tex=ORIGINAL)
