"""
Per-page PDF text extraction for chat-scoped Knowledge Base ingestion
(POST /tools/ingest-doc).

Mirrors the two-stage, fully-offline approach already used by the agent
service (agent/clients/pdf_extract.py) so the behavior is consistent:

  1. Direct text extraction with pdfplumber — works for normal text-based
     PDFs (a report exported from Word/Excel, etc). Returns text per page so
     each Knowledge Base chunk can carry a real page number for citations.
  2. OCR fallback (PyMuPDF renders each page to an image, pytesseract reads
     it) — only for genuinely SCANNED / image-only PDFs with no text layer.

pytesseract is a thin wrapper around the system `tesseract-ocr` binary,
which must be installed separately. If it (or PyMuPDF) is missing, OCR is
skipped and whatever direct text was found is returned — a scanned-only PDF
on a machine without tesseract simply yields no pages, and the caller turns
that into a clean "no extractable text" error rather than crashing.
"""
import base64
import io
from typing import List, Tuple

import pdfplumber

try:
    import pymupdf as fitz  # PyMuPDF
except ImportError:  # pragma: no cover - optional dependency
    fitz = None

try:
    import pytesseract
    from PIL import Image
except ImportError:  # pragma: no cover - optional dependency
    pytesseract = None
    Image = None

PDF_MIME_TYPES = ("application/pdf",)

# Below this many extracted characters across the whole document, the PDF is
# treated as having no real text layer (a handful of stray characters from a
# scanned page's artifacts) — worth trying OCR instead of trusting it.
_MIN_TOTAL_TEXT_TO_SKIP_OCR = 20


class PdfExtractionError(Exception):
    """The uploaded bytes could not be opened/parsed as a PDF at all."""


def is_pdf(mime_type: str | None, filename: str | None = None) -> bool:
    if mime_type and mime_type in PDF_MIME_TYPES:
        return True
    return bool(filename) and filename.lower().endswith(".pdf")


def _extract_direct_pages(pdf_bytes: bytes) -> List[Tuple[int, str]]:
    pages: List[Tuple[int, str]] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            page_text = (page.extract_text() or "").strip()
            if page_text:
                pages.append((i, page_text))
    return pages


def _extract_ocr_pages(pdf_bytes: bytes) -> List[Tuple[int, str]]:
    if fitz is None or pytesseract is None:
        print(
            "[PDF_INGEST] OCR fallback unavailable (PyMuPDF/pytesseract not "
            "installed, or tesseract-ocr missing) — no OCR text produced."
        )
        return []
    pages: List[Tuple[int, str]] = []
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        for i, page in enumerate(doc, start=1):
            pix = page.get_pixmap(dpi=200)
            image = Image.open(io.BytesIO(pix.tobytes("png")))
            page_text = pytesseract.image_to_string(image).strip()
            if page_text:
                pages.append((i, page_text))
        doc.close()
    except Exception as exc:  # noqa: BLE001 - OCR is best-effort only
        print(f"[PDF_INGEST] OCR fallback failed: {exc}")
        return []
    return pages


def extract_pdf_pages(file_base64: str) -> List[Tuple[int, str]]:
    """
    Returns [(page_number, page_text), ...] for the uploaded PDF — direct
    text if it has a real text layer, OCR'd text if it's scanned and
    tesseract is available. Returns [] if nothing could be extracted (the
    caller reports that as an empty/unreadable-PDF error).

    Raises PdfExtractionError only when the bytes are not a usable PDF at
    all (corrupted / not actually a PDF).
    """
    try:
        pdf_bytes = base64.b64decode(file_base64)
    except Exception as exc:  # noqa: BLE001
        raise PdfExtractionError(f"file is not valid base64: {exc}") from exc

    try:
        pages = _extract_direct_pages(pdf_bytes)
    except Exception as exc:  # noqa: BLE001 - pdfplumber raises many types on a bad file
        raise PdfExtractionError(f"could not open PDF (corrupted or not a PDF): {exc}") from exc

    total_chars = sum(len(t) for _, t in pages)
    if total_chars >= _MIN_TOTAL_TEXT_TO_SKIP_OCR:
        print(f"[PDF_INGEST] direct text layer found ({total_chars} chars over {len(pages)} page(s))")
        return pages

    print("[PDF_INGEST] no usable text layer -> trying OCR (scanned document path)")
    ocr_pages = _extract_ocr_pages(pdf_bytes)
    return ocr_pages or pages
