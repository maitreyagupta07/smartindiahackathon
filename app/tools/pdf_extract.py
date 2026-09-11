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


def has_no_text_layer(file_base64: str) -> bool:
    """
    True when the PDF has no real embedded/selectable text layer at all —
    i.e. it's a genuinely scanned/photographed document rather than an
    exported/typed one. Checks ONLY direct extraction (pdfplumber), never
    OCR — OCR's own success/quality must never affect this decision: a
    scanned page tesseract happens to read reasonably well is still a
    scanned page that may ALSO contain handwriting OCR missed, so the
    caller (app/agent/loop.py) still routes it through the local vision
    model in addition to whatever OCR text this module extracts.
    """
    try:
        pdf_bytes = base64.b64decode(file_base64)
        direct_text = _extract_direct_text(pdf_bytes)
    except Exception as exc:  # noqa: BLE001
        print(f"[PDF_EXTRACT] has_no_text_layer: could not check direct text ({exc}) -> treating as scanned")
        return True
    return len(direct_text) < _MIN_TEXT_LENGTH_TO_SKIP_OCR


def render_first_page_png_base64(file_base64: str) -> str | None:
    """
    Renders a scanned PDF's first page to a PNG and returns it as base64 —
    the same per-page rendering _extract_via_ocr already does, factored out
    so the agent loop can also hand this image to the local vision model
    (Moondream) when the PDF has no real text layer (see has_no_text_layer)
    and OCR alone can't be trusted to have caught everything — most notably
    handwritten notes/measurements/remarks, which Tesseract (tuned for
    printed text) reads unreliably. Only the first page: Moondream takes one
    image per call, and a multi-page vision pass is outside this fix's
    scope (see the caller's own docstring for that limitation). Returns
    None if PyMuPDF isn't installed or the PDF can't be opened/rendered —
    the caller then falls back to whatever text extract_text_from_pdf found.
    """
    if fitz is None:
        return None
    try:
        pdf_bytes = base64.b64decode(file_base64)
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        if doc.page_count == 0:
            doc.close()
            return None
        pix = doc[0].get_pixmap(dpi=200)
        png_bytes = pix.tobytes("png")
        doc.close()
        return base64.b64encode(png_bytes).decode("ascii")
    except Exception as exc:  # noqa: BLE001
        print(f"[PDF_EXTRACT] could not render first page to an image: {exc}")
        return None


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
