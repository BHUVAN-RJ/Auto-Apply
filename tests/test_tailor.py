"""Tailoring: block extraction, diffing, and the safety validations."""

import pytest

from tailor import tailor
from tailor.fetch import Posting

ORIGINAL = r"""\documentclass{article}
\begin{document}
Backend Engineer at Example Corp.
Built REST services.
\end{document}
"""


def reply(tex: str, rationale: str = "- swapped a term") -> str:
    return f"Here you go.\n\n```tex\n{tex}```\n\n```markdown\n{rationale}\n```\n"


def test_extract_block_reads_tex_and_markdown():
    text = reply(ORIGINAL)
    assert tailor._extract_block(text, ("tex", "latex")).startswith(r"\documentclass")
    assert tailor._extract_block(text, ("markdown", "md")) == "- swapped a term"


def test_extract_block_accepts_latex_fence():
    text = f"```latex\n{ORIGINAL}```"
    assert tailor._extract_block(text, ("tex", "latex")) is not None


def test_extract_block_returns_none_when_absent():
    assert tailor._extract_block("no fences here", ("tex",)) is None


def test_make_diff_is_a_unified_diff():
    after = ORIGINAL.replace("Built REST services.", "Built distributed REST services.")
    diff = tailor.make_diff(ORIGINAL, after)
    assert "-Built REST services." in diff
    assert "+Built distributed REST services." in diff
    assert diff.startswith("--- a/resume.tex")


def test_validate_accepts_a_reasonable_edit():
    after = ORIGINAL.replace("REST", "distributed REST")
    tailor._validate(ORIGINAL, after)  # must not raise


def test_validate_rejects_unbalanced_braces():
    with pytest.raises(tailor.TailorError, match="unbalanced braces"):
        tailor._validate(ORIGINAL, ORIGINAL + "\\textbf{oops")


def test_validate_rejects_a_dropped_document_environment():
    with pytest.raises(tailor.TailorError, match=r"\\end\{document\}"):
        tailor._validate(ORIGINAL, ORIGINAL.replace(r"\end{document}", ""))


def test_validate_rejects_a_wholesale_rewrite():
    with pytest.raises(tailor.TailorError, match="changed length too much"):
        tailor._validate(ORIGINAL, ORIGINAL + "\n" * 20)


def test_validate_rejects_empty_output():
    with pytest.raises(tailor.TailorError, match="empty"):
        tailor._validate(ORIGINAL, "   \n  ")


def test_tailor_end_to_end_with_a_stubbed_model(monkeypatch):
    after = ORIGINAL.replace("REST", "distributed REST")
    monkeypatch.setattr(tailor.llm, "complete", lambda *a, **k: reply(after))
    monkeypatch.setattr(tailor.llm, "tailor_model", lambda: "test/model")

    posting = Posting(url="https://example.com/j/1", text="We need distributed systems people. " * 20,
                      title="Backend Engineer", company="Example Corp")
    result = tailor.tailor(posting, resume_tex=ORIGINAL)

    assert "distributed REST" in result.tex
    assert result.model == "test/model"
    assert "+Built distributed REST services." in result.diff
    assert result.suggestions == "- swapped a term"


def test_tailor_raises_when_the_model_returns_no_tex(monkeypatch):
    monkeypatch.setattr(tailor.llm, "complete", lambda *a, **k: "I refuse.")
    monkeypatch.setattr(tailor.llm, "tailor_model", lambda: "test/model")
    posting = Posting(url="https://example.com/j/1", text="x")
    with pytest.raises(tailor.TailorError, match="no ```tex block"):
        tailor.tailor(posting, resume_tex=ORIGINAL)
