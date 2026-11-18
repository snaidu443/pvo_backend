"""Saves a `.pptx` as PDF with the locally installed Microsoft PowerPoint,
via the fixed script `export_pdf.ps1` (run by `office.run_export` with
`-File`, every value a separate parameter, no command string assembled
from input, no execution-policy override; the deck is opened read-only).

Why: the gateway gives the model only a PPTX's text (probed 2026-11-02),
while a PDF gets each page's image too. Windows + PowerPoint only; anything
else raises `PowerPointError` and the caller falls back.

Hidden slides are always left out (user decision 2026-11-03, no setting):
`prepare_copy` removes them from a temp copy before PowerPoint saves it, so
the PDF has one page per visible slide whatever PowerPoint's own export
option says, and the caller can check the page count against that.
"""

from __future__ import annotations

from pathlib import Path

from pptx import Presentation

from structx.source.office import OfficeError, run_export

_SCRIPT = Path(__file__).with_name("export_pdf.ps1")


class PowerPointError(OfficeError):
    pass


def hidden_slides(prs) -> list[int]:
    """1-based numbers of the slides marked hidden (`<p:sld show="0">`)."""
    return [n for n, slide in enumerate(prs.slides, start=1) if slide._element.get("show") == "0"]


def prepare_copy(pptx_path: Path, out_path: Path) -> Path:
    """Writes a copy of the deck WITHOUT its hidden slides to `out_path`
    (python-pptx: the slide is dropped from the slide list and its
    relationship, so its part is not written). The user's file is only
    read."""
    try:
        prs = Presentation(str(pptx_path))
        hidden = set(hidden_slides(prs))
        id_list = prs.slides._sldIdLst
        for n, sld_id in enumerate(list(id_list), start=1):
            if n in hidden:
                id_list.remove(sld_id)
                prs.part.drop_rel(sld_id.rId)
        prs.save(str(out_path))
    except Exception as exc:  # noqa: BLE001 - any failure here means no prepared copy
        raise PowerPointError(f"Could not prepare a copy of the deck: {type(exc).__name__}: {exc}") from exc
    return out_path


def export_pdf(pptx_path: Path, pdf_path: Path) -> Path:
    """Saves `pptx_path` as `pdf_path`, one page per visible slide (hidden
    slides left out, see `prepare_copy`). PowerPoint works on the copy
    placed next to `pdf_path` (the caller's temp dir)."""
    work_copy = prepare_copy(pptx_path, pdf_path.with_name("deck_copy.pptx"))
    try:
        return run_export(_SCRIPT, work_copy, pdf_path, app="PowerPoint")
    except OfficeError as exc:
        raise PowerPointError(str(exc)) from exc
