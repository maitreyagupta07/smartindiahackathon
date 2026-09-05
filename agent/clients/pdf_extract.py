"""
Extracts text from an uploaded PDF (contract §2.4's file_base64/
file_mime_type fields — "scanned PDFs" is explicitly called out as a
required multimodal input in the problem statement, but nothing in the
contract or codebase actually reads a PDF's content anywhere: only image
mime types are consumed today, by the vision (Moondream) path).

Two-stage extraction, entirely offline/air-gapped:
  1. Direct text extraction (pdfplumber) — works for normal, text-based
     PDFs (e.g. an inspection report exported from Word/Excel).
  2. OCR fallback (PyMuPDF renders each page to an image, pytesseract
     reads it) — needed for a genuinely SCANNED report, i.e. a PDF that's
     just photographed/scanned pages with no embedded text layer.

pytesseract is a thin wrapper — the actual OCR engine is the system
`tesseract-ocr` package, which must be installed on the machine (not
pip-installable). If it's missing, OCR is skipped and whatever direct text
was found (possibly none) is returned as-is — this must never crash the
agent loop just because a scanned-only PDF was uploaded on a machine
without tesseract installed.
"""
import base64
import io

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

# Below this many extracted characters, a PDF is treated as having no real
# text layer (e.g. a handful of stray characters from a scanned page's
# artifacts) — worth trying OCR rather than trusting the sparse text.
_MIN_TEXT_LENGTH_TO_SKIP_OCR = 20


def is_pdf(mime_type: str | None) -> bool:
    return bool(mime_type) and mime_type in PDF_MIME_TYPES


def _extract_direct_text(pdf_bytes: bytes) -> str:
    text_parts = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text() or ""
            if page_text.strip():
                text_parts.append(page_text.strip())
    return "\n\n".join(text_parts).strip()


def _extract_via_ocr(pdf_bytes: bytes) -> str:
    """
    Renders each page to an image with PyMuPDF and runs Tesseract OCR on
    it — this is what actually handles a SCANNED (photographed) report,
    where there's no text layer to extract directly at all.
    """
    if fitz is None or pytesseract is None:
        print(
            "[PDF_EXTRACT] OCR fallback unavailable (PyMuPDF/pytesseract not "
            "installed, or tesseract-ocr missing on this machine) — "
            "returning whatever direct text was found."
        )
        return ""

    text_parts = []
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        for page in doc:
            pix = page.get_pixmap(dpi=200)
            image = Image.open(io.BytesIO(pix.tobytes("png")))
            page_text = pytesseract.image_to_string(image)
            if page_text.strip():
                text_parts.append(page_text.strip())
        doc.close()
    except Exception as exc:  # noqa: BLE001
        print(f"[PDF_EXTRACT] OCR fallback failed: {exc}")
        return ""
    return "\n\n".join(text_parts).strip()


def extract_text_from_pdf(file_base64: str) -> str:
    """
    Returns the best-effort plain text content of the uploaded PDF —
    direct text if the PDF has a real text layer, OCR'd text if it's a
    scanned/image-only PDF and tesseract is available, or "" if neither
    produced anything (caller falls back to its existing no-file behavior).
    """
    try:
        pdf_bytes = base64.b64decode(file_base64)
    except Exception as exc:  # noqa: BLE001
        print(f"[PDF_EXTRACT] failed to decode file_base64: {exc}")
        return ""

    try:
        direct_text = _extract_direct_text(pdf_bytes)
    except Exception as exc:  # noqa: BLE001
        print(f"[PDF_EXTRACT] direct text extraction failed: {exc}")
        direct_text = ""

    if len(direct_text) >= _MIN_TEXT_LENGTH_TO_SKIP_OCR:
        print(f"[PDF_EXTRACT] direct text layer found ({len(direct_text)} chars) -> skipping OCR")
        return direct_text

    print("[PDF_EXTRACT] no usable text layer -> trying OCR (scanned document path)")
    ocr_text = _extract_via_ocr(pdf_bytes)
    return ocr_text or direct_text
