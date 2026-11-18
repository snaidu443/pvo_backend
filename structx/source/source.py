"""The input file: what is sent to the Responses API, and its own text per
page (read locally), which quotes and values are grounded against.

What the gateway shows the model differs by format (probed 2026-11-02 with
a synthetic deck):

- `.pdf` `input_file`: each page's text *and* image -- text inside pictures
  and charts is read, page numbers are right.
- `.pptx` `input_file`: text only -- slide text, tables, native chart data.
  Pictures, think-cell/OLE objects and drawn shapes are invisible, and a
  slide with no text is skipped, so later slide numbers shift.

So a `.pptx` is sent, by default (`STRUCTX_PPTX_MODE=pdf`), as **a PDF saved
by the local PowerPoint** (`powerpoint.py`; temp dir, deleted once read).
The model then sees every slide's image. Hidden slides are always LEFT OUT
(removed from the temp copy before the save, so page N of the PDF is the
N-th visible slide; their original numbers are kept in `hidden_removed`,
and `slide_of` maps a PDF page back to the deck's slide number -- the
written output uses slide numbers everywhere, user decision 2026-11-03)
and speaker notes are NOT sent and not used -- user decisions 2026-11-03:
keep it simple, the deck is the PDF. If PowerPoint fails or the page count
doesn't match the visible slides, it falls back to sending the `.pptx`
itself (also without its hidden slides), with a warning and `delivery`
recorded in the output.

A `.docx` (a protocol concept sheet) is always sent as **the PDF the local
Word saves from it** (`word.py`; temp copy, deleted once read), delivery
`docx_as_pdf`; the gateway cannot take a Word file, so there is no
fallback: without Word the run fails closed. Its own text for grounding
is that PDF's text layer, page numbers are the PDF's.

Local text for grounding: python-pptx (text frames, tables, group shapes,
native chart data) for a deck -- plus the exported PDF's text layer when
there is one -- and pypdfium2's text layer for a PDF. A quote is located by
normalized substring (case, whitespace, curly quotes, dashes, Markdown
table/emphasis marks folded; a standalone dash dropped), cited page first;
failing that, by the share of the quote covered by its longest contiguous
match.
"""

from __future__ import annotations

import base64
import logging
import tempfile
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

import pypdfium2 as pdfium
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE

from structx.model.llm import ExtractionError
from structx.source.powerpoint import PowerPointError, export_pdf, hidden_slides, prepare_copy
from structx.source.word import WordError
from structx.source.word import export_pdf as export_docx_pdf

_log = logging.getLogger(__name__)

PDF_MIME = "application/pdf"
PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
SUFFIXES = (".pdf", ".pptx", ".docx")
# OpenAI's documented per-file limit; the gateway's own is unknown.
MAX_FILE_BYTES = 50 * 1024 * 1024
FUZZY_MIN = 0.8
_MIN_QUOTE_CHARS = 3

_FOLD = {
    0x2018: "'", 0x2019: "'", 0x201C: '"', 0x201D: '"',
    0x2010: "-", 0x2011: "-", 0x2012: "-", 0x2013: "-", 0x2014: "-", 0x2212: "-",
    0x00A0: " ", 0x00B7: " ", 0x2022: " ",
    ord("*"): "", ord("#"): "", ord("\\"): "", ord("|"): " ", ord("`"): "",
}


def normalize(text: str) -> str:
    out: list[str] = []
    prev_space = True
    for i, ch in enumerate(text):
        for c in _FOLD.get(ord(ch), ch):
            # A dash standing alone ("Ph3 - Jan 2028") is punctuation the
            # model drops or swaps when quoting; one inside a word is kept.
            if c == "-" and prev_space and (i + 1 == len(text) or text[i + 1].isspace()):
                c = " "
            if c.isspace():
                if not prev_space:
                    out.append(" ")
                prev_space = True
            else:
                out.append(c.lower())
                prev_space = False
    return "".join(out).strip()


@dataclass
class Text:
    """One piece of the file's own text (a shape, a table row, a chart's
    data, a PDF page's text layer)."""
    text: str
    norm: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.norm = normalize(self.text)


@dataclass
class Match:
    found: str  # "exact" | "fuzzy" | "not_found"
    ratio: float
    page: int | None = None


@dataclass
class SourceFile:
    path: Path
    pages: dict[int, list[Text]]  # every page/slide, 1-based, even without text
    # What is sent: "pdf", "pptx", "pptx_as_pdf" or "docx_as_pdf".
    delivery: str
    payload: bytes = field(repr=False)
    payload_name: str
    payload_mime: str
    # The deck's hidden slides, left out of what is sent and of `pages`
    # (their original 1-based numbers; () for a PDF input).
    hidden_removed: tuple[int, ...] = ()

    @property
    def slide_count(self) -> int | None:
        """Slides in the deck, hidden ones included; None for a PDF or Word input."""
        return len(self.pages) + len(self.hidden_removed) if self.delivery.startswith("pptx") else None

    def slide_of(self, page: int) -> int:
        """The deck's slide number of a sent page (hidden slides before it
        were left out). Identity for a PDF input or a page that doesn't exist."""
        if not self.hidden_removed:
            return page
        visible = [n for n in range(1, self.slide_count + 1) if n not in self.hidden_removed]
        return visible[page - 1] if 1 <= page <= len(visible) else page

    @property
    def page_numbers_reliable(self) -> bool:
        """False only when a .pptx goes as-is: the gateway renumbers slides."""
        return self.delivery != "pptx"

    def content_parts(self) -> list[dict]:
        """The file as Responses API input parts: the one input_file part."""
        data = base64.b64encode(self.payload).decode("ascii")
        return [{"type": "input_file", "filename": self.payload_name, "file_data": f"data:{self.payload_mime};base64,{data}"}]

    def page_text(self, pages: set[int] | None = None) -> str:
        """The file's own text of `pages` (all pages if None)."""
        wanted = sorted(self.pages) if pages is None else sorted(p for p in pages if p in self.pages)
        return "\n".join(t.text for n in wanted for t in self.pages[n])

    def locate(self, quote: str, page: int | None) -> Match:
        needle = normalize(quote)
        if len(needle) < _MIN_QUOTE_CHARS:
            return Match("not_found", 0.0)
        order = ([page] if page in self.pages else []) + [n for n in sorted(self.pages) if n != page]
        for n in order:
            for t in self.pages[n]:
                if needle in t.norm:
                    return Match("exact", 1.0, n)
        best = Match("not_found", 0.0)
        for n in order:
            for t in self.pages[n]:
                m = SequenceMatcher(None, t.norm, needle, autojunk=False).find_longest_match(0, len(t.norm), 0, len(needle))
                ratio = m.size / len(needle)
                if ratio > best.ratio:  # strict: cited page wins ties
                    best = Match("fuzzy", round(ratio, 3), n)
        if best.ratio < FUZZY_MIN:
            return Match("not_found", round(best.ratio, 3))
        return best


def _shape_texts(shapes) -> list[str]:
    out: list[str] = []
    for shape in shapes:
        if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
            out += _shape_texts(shape.shapes)
            continue
        if shape.has_text_frame and shape.text_frame.text.strip():
            out.append(shape.text_frame.text)
        if getattr(shape, "has_table", False) and shape.has_table:
            for row in shape.table.rows:
                out.append(" | ".join(cell.text for cell in row.cells))
        if getattr(shape, "has_chart", False) and shape.has_chart:
            for plot in shape.chart.plots:
                cats = [str(c) for c in plot.categories]
                for series in plot.series:
                    values = ["" if v is None else f"{v:g}" for v in series.values]
                    out.append(f"{series.name} | " + " | ".join(f"{c} {v}" for c, v in zip(cats, values)))
    return out


def _pptx_pages(path: Path) -> tuple[dict[int, list[Text]], tuple[int, ...]]:
    """The deck's own text per PDF page, i.e. per VISIBLE slide (hidden
    slides and speaker notes are left out: they are not sent, so nothing may
    be grounded on them), and the hidden slides' original numbers."""
    prs = Presentation(str(path))
    hidden = hidden_slides(prs)
    visible = [slide for n, slide in enumerate(prs.slides, start=1) if n not in hidden]
    pages = {n: [Text(t) for t in _shape_texts(slide.shapes)] for n, slide in enumerate(visible, start=1)}
    return pages, tuple(hidden)


def _pdf_pages(path: Path) -> dict[int, list[Text]]:
    pages: dict[int, list[Text]] = {}
    pdf = pdfium.PdfDocument(str(path))
    try:
        for i in range(len(pdf)):
            text = pdf[i].get_textpage().get_text_range()
            pages[i + 1] = [Text(text)] if text.strip() else []
    finally:
        pdf.close()
    return pages


def _check_size(name: str, size: int) -> None:
    if size > MAX_FILE_BYTES:
        raise ExtractionError(f"{name} is {size / 1e6:.0f} MB, over the {MAX_FILE_BYTES // 1024 // 1024} MB file limit.")


def _docx_source(path: Path) -> SourceFile:
    """A Word document as the PDF Word saves from it (see the module docstring)."""
    _log.info("Saving %s as PDF with the local Word...", path.name)
    try:
        with tempfile.TemporaryDirectory(prefix="structx_") as tmp:
            pdf_path = export_docx_pdf(path, Path(tmp) / (path.stem + ".pdf"))
            pages = _pdf_pages(pdf_path)
            payload = pdf_path.read_bytes()
    except WordError as exc:
        raise ExtractionError(f"Could not save {path.name} as PDF: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 - any reader failure means we can't ground anything
        raise ExtractionError(f"Could not read {path.name}: {type(exc).__name__}: {exc}") from exc
    if not pages:
        raise ExtractionError(f"{path.name} gave an empty PDF.")
    _check_size(path.stem + ".pdf", len(payload))
    _log.info("Sending %s as PDF (%d page(s)).", path.name, len(pages))
    return SourceFile(path, pages, "docx_as_pdf", payload, path.stem + ".pdf", PDF_MIME)


def load_source(path: Path, *, pptx_mode: str = "pdf") -> SourceFile:
    suffix = path.suffix.lower()
    if suffix not in SUFFIXES:
        raise ExtractionError(f"Unsupported file type {suffix!r}: pass a .pdf, .pptx or .docx.")
    _check_size(path.name, path.stat().st_size)
    if suffix == ".docx":
        return _docx_source(path)
    try:
        if suffix == ".pdf":
            return SourceFile(path, _pdf_pages(path), "pdf", path.read_bytes(), path.name, PDF_MIME)
        pages, hidden = _pptx_pages(path)
    except Exception as exc:  # noqa: BLE001 - any reader failure means we can't ground anything
        raise ExtractionError(f"Could not read {path.name}: {type(exc).__name__}: {exc}") from exc
    if not pages:
        raise ExtractionError(f"{path.name} has no visible slides.")
    if hidden:
        _log.info("Hidden slide(s) %s left out; page N is the N-th visible slide.", list(hidden))

    if pptx_mode == "pdf":
        _log.info("Saving %s as PDF with the local PowerPoint (%d visible slide(s))...", path.name, len(pages))
        try:
            with tempfile.TemporaryDirectory(prefix="structx_") as tmp:
                pdf_path = export_pdf(path, Path(tmp) / (path.stem + ".pdf"))
                pdf_pages = _pdf_pages(pdf_path)
                payload = pdf_path.read_bytes()
            if len(pdf_pages) != len(pages):
                raise PowerPointError(f"the PDF has {len(pdf_pages)} page(s) for {len(pages)} visible slide(s)")
            _check_size(path.stem + ".pdf", len(payload))
            # The PDF's text layer too: what PowerPoint actually drew
            # (e.g. autofit-wrapped or field text) can differ from the XML.
            for n, texts in pdf_pages.items():
                pages[n] += texts
            _log.info("Sending %s as PDF (%d page(s)).", path.name, len(pages))
            return SourceFile(path, pages, "pptx_as_pdf", payload, path.stem + ".pdf", PDF_MIME, hidden)
        except PowerPointError as exc:
            _log.warning(
                "Could not save %s as PDF (%s); sending the .pptx itself -- the model will see its text "
                "only, not pictures or charts drawn as images.", path.name, exc,
            )
    payload = path.read_bytes()
    if hidden:
        # Sent as-is, but never with the hidden slides: a temp copy without them.
        try:
            with tempfile.TemporaryDirectory(prefix="structx_") as tmp:
                payload = prepare_copy(path, Path(tmp) / "deck_copy.pptx").read_bytes()
        except PowerPointError as exc:
            # Fail closed: the hidden slides are never sent.
            raise ExtractionError(f"Could not leave the hidden slides out of {path.name}: {exc}") from exc
    return SourceFile(path, pages, "pptx", payload, path.name, PPTX_MIME, hidden)
