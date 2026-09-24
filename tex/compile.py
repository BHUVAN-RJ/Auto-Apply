r"""LaTeX wrapper.

Compiles a .tex file to PDF in an isolated directory so the aux, log, and out
files never land next to the archived artifacts. Runs twice, because resume
templates that use page-total references or tabular width calculations need a
second pass to settle.

Tectonic is the engine an installed app gets: one binary, packages fetched on
first use, no TeX distribution to install. It is used when the engine the
document wants is not installed (or `AUTOPILOT_TEX_ENGINE=tectonic`). It runs
XeTeX, so the pdfTeX-only lines the common templates carry
(`\input{glyphtounicode}`, `\pdfgentounicode=1`) are given harmless
definitions first; they only tune copy-paste out of the PDF.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

# BasicTeX and MacTeX both land here, and it is not on a non-login shell's PATH.
TEXBIN = "/Library/TeX/texbin"
PASSES = 2
TIMEOUT = 120

# Engines, in the order they are tried when a document does not say which it
# needs. pdflatex first because the common resume templates are built for it:
# they use \input{glyphtounicode} and \pdfgentounicode, which are pdfTeX
# primitives that LuaTeX does not provide.
ENGINES = ("pdflatex", "lualatex", "xelatex")

TECTONIC = "tectonic"
# Ahead of \documentclass under Tectonic: the pdfTeX primitives resume
# templates use for text extraction, defined to do nothing under XeTeX.
PDFTEX_SHIM = (
    "\\ifdefined\\pdfgentounicode\\else\\newcount\\pdfgentounicode\\fi\n"
    "\\ifdefined\\pdfglyphtounicode\\else\\def\\pdfglyphtounicode#1#2{}\\fi\n"
)

# Packages that only work under an engine with native Unicode font handling.
UNICODE_ENGINE_MARKERS = (
    r"\usepackage{fontspec}",
    r"\setmainfont",
    r"\usepackage{polyglossia}",
    r"\usepackage{unicode-math}",
)


class CompileError(RuntimeError):
    """Raised when lualatex exits non-zero. Carries the log for diagnosis."""

    def __init__(self, message: str, log: str = "") -> None:
        missing = missing_packages(log)
        if missing:
            names = " ".join(sorted(missing))
            message = (
                f"{message}: missing TeX package(s) {names}. "
                f"BasicTeX is minimal; install them with: sudo tlmgr install {names}"
            )
        super().__init__(message)
        self.log = log
        self.missing_packages = missing

    def tail(self, lines: int = 40) -> str:
        return "\n".join(self.log.splitlines()[-lines:])


def missing_packages(log: str) -> list[str]:
    """Package names behind "File `foo.sty\' not found" errors in a TeX log.

    BasicTeX ships a minimal package set, so a resume template pulling in
    anything beyond the basics fails this way. The .sty name is usually but
    not always the tlmgr package name; it is right often enough to be the
    useful thing to put in front of the user.
    """
    found = re.findall(r"File `([^']+)\.(?:sty|cls)' not found", log)
    return sorted(set(found))


def find_engine(name: str) -> Optional[str]:
    return shutil.which(name) or shutil.which(name, path=TEXBIN)


def find_lualatex() -> Optional[str]:
    return find_engine("lualatex")


def choose_engine(source: str) -> str:
    """Pick the engine this document needs.

    fontspec and friends require lualatex or xelatex. Everything else gets
    pdflatex, which is what the widely-copied resume templates assume.
    """
    override = os.environ.get("AUTOPILOT_TEX_ENGINE")
    if override:
        return override
    # Commented-out lines do not count. Resume templates carry a block of
    # alternative font choices commented out at the top, and matching one of
    # those would pick an engine the document cannot actually compile under.
    active = strip_comments(source)
    if any(marker in active for marker in UNICODE_ENGINE_MARKERS):
        return "lualatex"
    return "pdflatex"


def strip_comments(source: str) -> str:
    """Drop TeX comments, respecting the escaped percent sign."""
    return "\n".join(
        re.split(r"(?<!\\)%", line)[0] for line in source.splitlines()
    )


def compile_pdf(
    tex_path: Path,
    out_pdf: Path,
    extra_inputs: Optional[list[Path]] = None,
) -> Path:
    """Compile `tex_path` and place the PDF at `out_pdf`.

    `extra_inputs` are copied alongside the source: .cls, .sty, fonts, any
    image the template pulls in.
    """
    tex_path = Path(tex_path).resolve()
    engine_name = choose_engine(tex_path.read_text(errors="replace"))
    engine = find_engine(engine_name)
    if engine is None and engine_name != TECTONIC:
        engine = find_engine(TECTONIC)
        engine_name = TECTONIC if engine else engine_name
    if engine is None:
        raise CompileError(
            f"{engine_name} not found. Install it with: brew install tectonic"
        )

    out_pdf = Path(out_pdf).resolve()
    out_pdf.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="autopilot-tex-") as tmp:
        work = Path(tmp)
        source = work / "document.tex"
        shutil.copy(tex_path, source)

        # Anything sitting next to the source is fair game as a dependency,
        # which covers the common case of a .cls beside the resume.
        for sibling in tex_path.parent.iterdir():
            if sibling.is_file() and sibling != tex_path:
                shutil.copy(sibling, work / sibling.name)
        for extra in extra_inputs or []:
            extra = Path(extra)
            if extra.is_dir():
                shutil.copytree(extra, work / extra.name, dirs_exist_ok=True)
            else:
                shutil.copy(extra, work / extra.name)

        env = os.environ.copy()
        env["PATH"] = f"{TEXBIN}:{env.get('PATH', '')}"

        log = ""
        if engine_name == TECTONIC:
            source.write_text(PDFTEX_SHIM + source.read_text(errors="replace"))
            # Tectonic reruns until the document settles by itself. The
            # first run downloads the packages it needs, hence the timeout.
            proc = subprocess.run(
                [engine, "-X", "compile", "--keep-logs", "document.tex"],
                cwd=work, env=env, capture_output=True, text=True,
                timeout=TIMEOUT * 5,
            )
            log_file = work / "document.log"
            log = log_file.read_text(errors="replace") if log_file.exists() else proc.stderr
            if proc.returncode != 0:
                raise CompileError(f"tectonic failed on {tex_path.name}", log + proc.stderr)
        for _ in range(0 if engine_name == TECTONIC else PASSES):
            proc = subprocess.run(
                [
                    engine,
                    "-interaction=nonstopmode",
                    "-halt-on-error",
                    "-file-line-error",
                    "document.tex",
                ],
                cwd=work,
                env=env,
                capture_output=True,
                text=True,
                timeout=TIMEOUT,
            )
            log_file = work / "document.log"
            log = log_file.read_text(errors="replace") if log_file.exists() else proc.stdout
            if proc.returncode != 0:
                raise CompileError(f"{engine_name} failed on {tex_path.name}", log)

        produced = work / "document.pdf"
        if not produced.exists():
            raise CompileError(f"{engine_name} reported success but produced no PDF", log)
        shutil.copy(produced, out_pdf)

    return out_pdf


def page_count(pdf_path: Path) -> Optional[int]:
    """Page count, or None if the file cannot be parsed.

    Counting `/Type /Page` markers in the raw bytes does not work here:
    lualatex compresses its object streams, so the markers are not in the
    file as plain text. pypdf reads the real page tree.
    """
    try:
        from pypdf import PdfReader
    except ImportError:
        return None
    try:
        return len(PdfReader(str(pdf_path)).pages)
    except Exception:  # noqa: BLE001 - a malformed PDF is "unknown", not fatal
        return None
