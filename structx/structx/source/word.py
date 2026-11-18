"""Saves a `.docx` as PDF with the locally installed Microsoft Word, via the
fixed script `export_pdf_word.ps1` (see `office.run_export`: `-File`,
every value a separate parameter, the document opened read-only).

Why: the gateway takes a PDF `input_file` (each page's text and image) but
not a Word document, so a protocol concept sheet is sent as the PDF Word
saves from it, and that PDF's own text layer is what quotes are grounded
against (`source.load_source`). Word works on a temp copy of the user's
file; nothing is written next to it. No fallback: if Word is not there or
fails, the run fails closed (`WordError`), since the `.docx` itself cannot
be sent.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from structx.source.office import OfficeError, run_export

_SCRIPT = Path(__file__).with_name("export_pdf_word.ps1")


class WordError(OfficeError):
    pass


def export_pdf(docx_path: Path, pdf_path: Path) -> Path:
    """Saves `docx_path` as `pdf_path`. Word opens a copy placed next to
    `pdf_path` (the caller's temp dir), read-only, without a window."""
    try:
        work_copy = Path(shutil.copyfile(docx_path, pdf_path.with_name("doc_copy.docx")))
        return run_export(_SCRIPT, work_copy, pdf_path, app="Word")
    except OfficeError as exc:
        raise WordError(str(exc)) from exc
    except OSError as exc:
        raise WordError(f"Could not copy {docx_path.name} for Word: {exc}") from exc
