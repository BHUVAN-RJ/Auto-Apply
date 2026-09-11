"""lualatex wrapper. The compile tests skip cleanly when TeX is not installed."""

from pathlib import Path

import pytest

from tex import compile as texc

needs_tex = pytest.mark.skipif(
    texc.find_lualatex() is None,
    reason="lualatex not installed (brew install --cask basictex)",
)

MINIMAL = r"""\documentclass{article}
\pagestyle{empty}
\begin{document}
Hello.
\end{document}
"""

BROKEN = r"""\documentclass{article}
\begin{document}
\thisCommandDoesNotExist
\end{document}
"""


def test_a_missing_engine_gives_an_actionable_error(tmp_path, monkeypatch):
    monkeypatch.setattr(texc, "find_engine", lambda name: None)
    src = tmp_path / "doc.tex"
    src.write_text(MINIMAL)
    with pytest.raises(texc.CompileError, match="basictex"):
        texc.compile_pdf(src, tmp_path / "out.pdf")


@needs_tex
def test_compiles_a_minimal_document(tmp_path):
    src = tmp_path / "doc.tex"
    src.write_text(MINIMAL)
    pdf = texc.compile_pdf(src, tmp_path / "out" / "resume.pdf")
    assert pdf.exists()
    assert pdf.read_bytes().startswith(b"%PDF")
    assert texc.page_count(pdf) == 1


@needs_tex
def test_leaves_no_aux_files_beside_the_source(tmp_path):
    src = tmp_path / "doc.tex"
    src.write_text(MINIMAL)
    texc.compile_pdf(src, tmp_path / "out" / "resume.pdf")
    assert not list(tmp_path.glob("*.aux"))
    assert not list(tmp_path.glob("*.log"))


@needs_tex
def test_compile_failure_carries_the_log(tmp_path):
    src = tmp_path / "doc.tex"
    src.write_text(BROKEN)
    with pytest.raises(texc.CompileError) as excinfo:
        texc.compile_pdf(src, tmp_path / "out.pdf")
    assert "thisCommandDoesNotExist" in excinfo.value.log


@needs_tex
def test_compiles_the_example_resume(tmp_path):
    example = Path(__file__).resolve().parent.parent / "base" / "resume.example.tex"
    pdf = texc.compile_pdf(example, tmp_path / "resume.pdf")
    assert texc.page_count(pdf) == 1


def test_page_count_returns_none_for_a_non_pdf(tmp_path):
    junk = tmp_path / "not.pdf"
    junk.write_text("not a pdf")
    assert texc.page_count(junk) is None


def test_missing_package_error_names_the_tlmgr_command():
    log = "! LaTeX Error: File `enumitem.sty' not found.\n"
    err = texc.CompileError("lualatex failed", log)
    assert err.missing_packages == ["enumitem"]
    assert "sudo tlmgr install enumitem" in str(err)


def test_missing_packages_is_empty_for_an_unrelated_failure():
    assert texc.missing_packages("! Undefined control sequence.") == []
