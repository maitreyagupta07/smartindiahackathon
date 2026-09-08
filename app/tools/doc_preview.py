"""
Server-side, fully-offline document preview rendering.

Turns the raw bytes of a generated deliverable or a Knowledge Base upload
into something the frontend's right-side preview panel can display inline:

  .pdf              -> {"mode": "pdf"}      (panel embeds the raw file itself)
  .docx             -> {"mode": "html", "html": ...}  (headings, paragraphs, tables)
  .xlsx / .xls      -> {"mode": "html", "html": ...}  (one <table> per sheet)
  .pptx             -> {"mode": "html", "html": ...}  (one block per slide)
  .txt / .md        -> {"mode": "text", "text": ...}
  anything else      -> {"mode": "unsupported"}

Reuses the exact same parser libraries (python-docx / openpyxl / python-pptx)
already pinned for filegen + KB ingestion — no new dependency, no network,
no headless-office conversion step. The HTML produced here is assembled from
escaped cell/paragraph text only; it never echoes raw file bytes.
"""
import base64
import html
import io
from pathlib import Path
from typing import Optional

# Defensive caps so a huge spreadsheet/deck can't produce a multi-megabyte
# preview payload or hang the request building one.
_MAX_TABLE_ROWS = 400
_MAX_TABLE_COLS = 40
_MAX_SLIDES = 200
_MAX_TEXT_CHARS = 200_000

_PREVIEWABLE_EXTS = (".pdf", ".docx", ".xlsx", ".xls", ".pptx", ".txt", ".md")


def is_previewable(filename: str) -> bool:
    return Path(filename or "").suffix.lower() in _PREVIEWABLE_EXTS


def _esc(value) -> str:
    return html.escape("" if value is None else str(value))


def _render_docx(raw: bytes) -> str:
    from docx import Document  # python-docx

    doc = Document(io.BytesIO(raw))
    parts: list[str] = []
    for para in doc.paragraphs:
        text = (para.text or "").strip()
        if not text:
            continue
        style = (para.style.name if para.style else "") or ""
        if style.startswith("Heading 1") or style == "Title":
            parts.append(f"<h2>{_esc(text)}</h2>")
        elif style.startswith("Heading"):
            parts.append(f"<h3>{_esc(text)}</h3>")
        elif style.startswith("List"):
            parts.append(f"<li>{_esc(text)}</li>")
        else:
            parts.append(f"<p>{_esc(text)}</p>")

    for table in doc.tables:
        rows_html: list[str] = []
        for r, row in enumerate(table.rows):
            if r >= _MAX_TABLE_ROWS:
                rows_html.append(f'<tr><td>… {len(table.rows) - _MAX_TABLE_ROWS} more row(s)</td></tr>')
                break
            cells = row.cells[:_MAX_TABLE_COLS]
            tag = "th" if r == 0 else "td"
            rows_html.append(
                "<tr>" + "".join(f"<{tag}>{_esc((c.text or '').strip())}</{tag}>" for c in cells) + "</tr>"
            )
        if rows_html:
            parts.append(f'<table class="preview-table">{"".join(rows_html)}</table>')

    return "".join(parts) or "<p class='preview-empty'>This document has no extractable text.</p>"


def _render_xlsx(raw: bytes) -> str:
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    blocks: list[str] = []
    for ws in wb.worksheets:
        rows_html: list[str] = []
        for r, row in enumerate(ws.iter_rows(values_only=True)):
            if r >= _MAX_TABLE_ROWS:
                rows_html.append(f'<tr><td>… more rows not shown</td></tr>')
                break
            cells = list(row)[:_MAX_TABLE_COLS]
            if not any(("" if v is None else str(v)).strip() for v in cells):
                continue
            tag = "th" if r == 0 else "td"
            rows_html.append(
                "<tr>" + "".join(f"<{tag}>{_esc('' if v is None else v)}</{tag}>" for v in cells) + "</tr>"
            )
        blocks.append(
            f"<h3>{_esc(ws.title)}</h3>"
            + (f'<table class="preview-table">{"".join(rows_html)}</table>'
               if rows_html else "<p class='preview-empty'>(empty sheet)</p>")
        )
    wb.close()
    return "".join(blocks) or "<p class='preview-empty'>This workbook has no sheets.</p>"


def _render_pptx(raw: bytes) -> str:
    from pptx import Presentation  # python-pptx

    prs = Presentation(io.BytesIO(raw))
    blocks: list[str] = []
    for idx, slide in enumerate(prs.slides, start=1):
        if idx > _MAX_SLIDES:
            break
        lines: list[str] = []
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False):
                for para in shape.text_frame.paragraphs:
                    line = "".join(run.text for run in para.runs).strip()
                    if line:
                        lines.append(f"<p>{_esc(line)}</p>")
            if getattr(shape, "has_table", False):
                rows_html = []
                for r, trow in enumerate(shape.table.rows):
                    tag = "th" if r == 0 else "td"
                    rows_html.append(
                        "<tr>" + "".join(f"<{tag}>{_esc((c.text or '').strip())}</{tag}>" for c in trow.cells) + "</tr>"
                    )
                if rows_html:
                    lines.append(f'<table class="preview-table">{"".join(rows_html)}</table>')
        body = "".join(lines) or "<p class='preview-empty'>(no text on this slide)</p>"
        blocks.append(f'<section class="preview-slide"><div class="preview-slide-no">Slide {idx}</div>{body}</section>')
    return "".join(blocks) or "<p class='preview-empty'>This presentation has no slides.</p>"


def render_preview(raw: bytes, filename: str, mime: Optional[str] = None) -> dict:
    """
    Returns one of:
      {"mode": "pdf"}
      {"mode": "html", "html": "<...>"}
      {"mode": "text", "text": "..."}
      {"mode": "unsupported"}
      {"mode": "error", "message": "..."}
    The caller adds `raw_url`, `filename` and `file_type` around this.
    """
    ext = Path(filename or "").suffix.lower()
    try:
        if ext == ".pdf":
            return {"mode": "pdf"}
        if ext == ".docx":
            return {"mode": "html", "html": _render_docx(raw)}
        if ext in (".xlsx", ".xls"):
            return {"mode": "html", "html": _render_xlsx(raw)}
        if ext == ".pptx":
            return {"mode": "html", "html": _render_pptx(raw)}
        if ext in (".txt", ".md"):
            return {"mode": "text", "text": raw.decode("utf-8", errors="ignore")[:_MAX_TEXT_CHARS]}
        return {"mode": "unsupported"}
    except Exception as exc:  # noqa: BLE001 - parser libs raise many types on a bad file
        return {"mode": "error", "message": f"Could not render a preview of this file ({type(exc).__name__})."}


def render_preview_b64(file_base64: str, filename: str, mime: Optional[str] = None) -> dict:
    try:
        raw = base64.b64decode(file_base64)
    except Exception:  # noqa: BLE001
        return {"mode": "error", "message": "file is not valid base64"}
    return render_preview(raw, filename, mime)
