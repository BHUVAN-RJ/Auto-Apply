"""lualatex wrapper.

Compiles a .tex file to PDF in an isolated directory so the aux, log, and out
files never land next to the archived artifacts. Runs twice, because resume
templates that use page-total references or tabular width calculations need a
second pass to settle.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

# BasicTeX and MacTeX both land here, and it is not on a non-login shell's PATH.
TEXBIN = "/Library/TeX/texbin"
PASSES = 2
TIMEOUT = 120


class CompileError(RuntimeError):
    """Raised when lualatex exits non-zero. Carries the log for diagnosis."""

    def __init__(self, message: str, log: str = "") -> None:
        super().__init__(message)
        self.log = log

    def tail(self, lines: int = 40) -> str:
        return "\n".join(self.log.splitlines()[-lines:])


def find_lualatex() -> Optional[str]:
    return shutil.which("lualatex") or shutil.which("lualatex", path=TEXBIN)


def compile_pdf(
    tex_path: Path,
    out_pdf: Path,
    extra_inputs: Optional[list[Path]] = None,
) -> Path:
    """Compile `tex_path` and place the PDF at `out_pdf`.

    `extra_inputs` are copied alongside the source: .cls, .sty, fonts, any
    image the template pulls in.
    """
    lualatex = find_lualatex()
    if lualatex is None:
        raise CompileError(
            "lualatex not found. Install it with: brew install --cask basictex"
        )

    tex_path = Path(tex_path).resolve()
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
        for _ in range(PASSES):
            proc = subprocess.run(
                [
                    lualatex,
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
                raise CompileError(f"lualatex failed on {tex_path.name}", log)

        produced = work / "document.pdf"
        if not produced.exists():
            raise CompileError("lualatex reported success but produced no PDF", log)
        shutil.copy(produced, out_pdf)

    return out_pdf


def page_count(pdf_path: Path) -> Optional[int]:
    """Page count without a PDF library, by counting /Type /Page objects.

    Returns None if the file cannot be parsed; callers treat that as unknown
    rather than as a failure.
    """
    try:
        data = Path(pdf_path).read_bytes()
    except OSError:
        return None
    count = data.count(b"/Type /Page") - data.count(b"/Type /Pages")
    return count if count > 0 else None
