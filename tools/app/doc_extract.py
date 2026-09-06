"""
Multi-format text extraction for chat-scoped Knowledge Base ingestion
(POST /tools/ingest-doc).

PDF keeps its dedicated two-stage path (direct text -> OCR fallback) in
pdf_ingest.py. This module adds the office formats on top of it so a user
can drop a Word / PowerPoint / Excel file — or a plain .txt/.md — straight
into a chat's Knowledge Base:

  .pdf              -> pdf_ingest.extract_pdf_pages  (per real page)
  .docx             -> python-docx    (paragraphs + table cells, one "page")
  .pptx             -> python-pptx    (one "page" per slide, keeps slide no.)
  .xlsx / .xls      -> openpyxl       (one "page" per sheet)
  .txt / .md        -> utf-8 decode   (one "page")

Every extractor returns the same [(page_number, page_text), ...] shape that
docsearch.ingest_chat_document already consumes, so nothing downstream
changes. All parsing is pure-pip and fully offline.
"""
import base64
import io
from typing import List, Tuple

from . import pdf_ingest

DOC_MIME_HINTS = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/msword": ".doc",
    "application/vnd.ms-excel": ".xls",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "application/pdf": ".pdf",
}

# Extensions this service can turn into Knowledge Base text.
SUPPORTED_EXTENSIONS = (".pdf", ".docx", ".pptx", ".xlsx", ".xls", ".txt", ".md")


class DocExtractionError(Exception):
    """The uploaded bytes could not be parsed as the format they claim to be."""


def _ext_for(mime_type: str | None, filename: str | None) -> str:
    name = (filename or "").lower()
    for ext in SUPPORTED_EXTENSIONS:
        if name.endswith(ext):
            return ext
    return DOC_MIME_HINTS.get((mime_type or "").lower(), "")


def is_supported(mime_type: str | None, filename: str | None = None) -> bool:
    return _ext_for(mime_type, filename) in SUPPORTED_EXTENSIONS


def describe_supported() -> str:
    return "PDF, Word (.docx), PowerPoint (.pptx), Excel (.xlsx/.xls), or plain text (.txt/.md)"


def _decode(file_base64: str) -> bytes:
    try:
        return base64.b64decode(file_base64)
    except Exception as exc:  # noqa: BLE001
        raise DocExtractionError(f"file is not valid base64: {exc}") from exc


def _extract_docx(raw: bytes) -> List[Tuple[int, str]]:
    from docx import Document  # python-docx

    doc = Document(io.BytesIO(raw))
    parts = [p.text for p in doc.paragraphs if p.text and p.text.strip()]
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text and c.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    text = "\n".join(parts).strip()
    return [(1, text)] if text else []


def _extract_pptx(raw: bytes) -> List[Tuple[int, str]]:
    from pptx import Presentation  # python-pptx

    prs = Presentation(io.BytesIO(raw))
    pages: List[Tuple[int, str]] = []
    for slide_no, slide in enumerate(prs.slides, start=1):
        lines: List[str] = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                for para in shape.text_frame.paragraphs:
                    line = "".join(run.text for run in para.runs).strip()
                    if line:
                        lines.append(line)
            if shape.has_table:
                for row in shape.table.rows:
                    cells = [c.text.strip() for c in row.cells if c.text and c.text.strip()]
                    if cells:
                        lines.append(" | ".join(cells))
        slide_text = "\n".join(lines).strip()
        if slide_text:
            pages.append((slide_no, f"[Slide {slide_no}]\n{slide_text}"))
    return pages


def _extract_xlsx(raw: bytes) -> List[Tuple[int, str]]:
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    pages: List[Tuple[int, str]] = []
    for sheet_no, ws in enumerate(wb.worksheets, start=1):
        rows: List[str] = []
        for row in ws.iter_rows(values_only=True):
            cells = ["" if v is None else str(v) for v in row]
            if any(c.strip() for c in cells):
                rows.append(" | ".join(cells).rstrip(" |"))
        sheet_text = "\n".join(rows).strip()
        if sheet_text:
            pages.append((sheet_no, f"[Sheet: {ws.title}]\n{sheet_text}"))
    wb.close()
    return pages


def _extract_text(raw: bytes) -> List[Tuple[int, str]]:
    text = raw.decode("utf-8", errors="ignore").strip()
    return [(1, text)] if text else []


def extract_pages(file_base64: str, mime_type: str | None, filename: str | None) -> List[Tuple[int, str]]:
    """
    Returns [(page_number, page_text), ...] for the uploaded document.
    Raises DocExtractionError if the bytes are unreadable / not the claimed
    format. An empty list means "opened fine but held no extractable text"
    — the caller turns that into a clean 400.
    """
    ext = _ext_for(mime_type, filename)
    if ext == ".pdf":
        try:
            return pdf_ingest.extract_pdf_pages(file_base64)
        except pdf_ingest.PdfExtractionError as exc:
            raise DocExtractionError(str(exc)) from exc

    raw = _decode(file_base64)
    try:
        if ext == ".docx":
            return _extract_docx(raw)
        if ext == ".pptx":
            return _extract_pptx(raw)
        if ext in (".xlsx", ".xls"):
            return _extract_xlsx(raw)
        if ext in (".txt", ".md"):
            return _extract_text(raw)
    except DocExtractionError:
        raise
    except Exception as exc:  # noqa: BLE001 - parser libs raise many types on a bad file
        raise DocExtractionError(
            f"could not read {ext or 'file'} (corrupted or not a real {ext or 'document'}): {exc}"
        ) from exc

    raise DocExtractionError(
        f"unsupported file type {mime_type or filename!r} — supported: {describe_supported()}"
    )
