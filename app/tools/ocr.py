"""
Scans a photographed document (handwritten field notes, an inspection
sheet, a whiteboard, a nameplate) and returns its transcribed text via
Tesseract OCR — the same engine app/tools/pdf_extract.py already uses for
scanned PDFs, applied here directly to an uploaded image instead.

Why this exists as its own tool, separate from the vision (Moondream)
path: Moondream is a vision-LANGUAGE model — good at describing a photo
("a gauge reading approximately 45 PSI", "a hand holding a wrench near a
valve"), but it is not trained for character-accurate transcription and
will paraphrase or hallucinate exact wording rather than transcribe it
faithfully. Tesseract does the opposite: no scene understanding at all,
but it reads the actual pixels character-by-character. For "what does
this note say, word for word" requests, that's the tool you want.

Runs entirely on CPU — no Ollama call, no GPU/VRAM usage at all. This
matters on this machine specifically: the RTX 3050's 4GB is already
committed to qwen2.5:1.5b-instruct + moondream with limited headroom (see
inference/README.md), so adding OCR as a plain CPU tool costs nothing on
the budget that's actually tight, instead of trying to also fit a bigger
vision-language model.

Honesty about limits (per this project's own no-fabrication standard):
Tesseract is tuned for printed/typed text. It is usable but genuinely
weaker on messy cursive handwriting — the caller (planner.py) always
passes the raw OCR output through a Qwen cleanup step afterward, and
Qwen is explicitly told this is unverified raw OCR text and to say so if
it looks garbled, rather than presenting noisy OCR as a confident
transcription.
"""
import base64
import io

try:
    import pytesseract
    from PIL import Image, ImageOps
except ImportError:  # pragma: no cover - optional dependency
    pytesseract = None
    Image = None
    ImageOps = None

# Tesseract's page-segmentation mode: 6 = "assume a single uniform block of
# text" — the right default for a photographed note/page rather than a
# full-layout document with columns/tables (mode 3, tesseract's default).
_TESSERACT_CONFIG = "--psm 6"


def is_available() -> bool:
    """False if PyMuPDF's OCR sibling deps aren't installed, or the system
    `tesseract-ocr` binary itself is missing — checked lazily (calling
    pytesseract triggers the actual binary lookup), never at import time."""
    if pytesseract is None or Image is None:
        return False
    try:
        pytesseract.get_tesseract_version()
        return True
    except Exception:  # noqa: BLE001 - binary missing or broken install
        return False


def _preprocess(image: "Image.Image") -> "Image.Image":
    """Cheap, well-known OCR accuracy boosts — grayscale + autocontrast,
    and upscaling a small photo — before handing it to Tesseract. No
    heavyweight image-processing dependency added for this."""
    image = image.convert("L")  # grayscale
    image = ImageOps.autocontrast(image)
    if image.width < 1000:
        scale = 1000 / image.width
        image = image.resize((int(image.width * scale), int(image.height * scale)))
    return image


def scan_handwritten_image(image_base64: str) -> dict:
    """
    Returns {"text": str, "char_count": int, "available": bool}.

    "available": False (with "text": "") means Tesseract itself isn't
    installed on this machine — the caller must not treat that as "the
    note was blank", and the planner degrades gracefully the same way
    execute_code does when Docker is unavailable, instead of failing the
    whole task.
    """
    if not is_available():
        print("[OCR] scan_handwritten_image UNAVAILABLE (tesseract-ocr not installed on this host)")
        return {"text": "", "char_count": 0, "available": False}

    try:
        image_bytes = base64.b64decode(image_base64)
        image = Image.open(io.BytesIO(image_bytes))
        image = _preprocess(image)
        text = pytesseract.image_to_string(image, config=_TESSERACT_CONFIG).strip()
    except Exception as exc:  # noqa: BLE001 - a bad/corrupt image must not crash the task
        print(f"[OCR] scan_handwritten_image FAILED: {exc}")
        return {"text": "", "char_count": 0, "available": True, "error": str(exc)}

    print(f"[OCR] scan_handwritten_image done char_count={len(text)}")
    return {"text": text, "char_count": len(text), "available": True}
