"""
Persistent, per-operator global Knowledge Base + inline document preview.

Two capabilities the base chat-scoped KB (app/api/chat.py) does not cover:

  1. A KB that is in retrieval scope for EVERY chat the operator opens and
     that persists until they explicitly remove a file — managed from the
     sidebar (add / remove / search). Chunks live in docsearch's `global_kb`
     collection, tagged with the operator's user_id; the original bytes are
     kept under KB_STORE_DIR so a file can be previewed/downloaded later.

  2. A server-rendered, fully-offline preview (app/tools/doc_preview.py) of
     any generated deliverable or KB file, so the frontend can show a
     Word/Excel/PowerPoint/PDF/text document inline in a right-side panel
     instead of only offering a download.
"""
import base64
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from ..audit.log import write_audit_entry
from ..storage.config import FILES_DIR, KB_STORE_DIR
from ..tools.doc_preview import render_preview
from ..tools.facade import (
    ingest_global_document,
    list_global_kb_documents,
    delete_global_kb_document,
    DocumentIngestError,
)

router = APIRouter()

_KB_EXTS = (".pdf", ".docx", ".pptx", ".xlsx", ".xls", ".txt", ".md")


class KbUploadRequest(BaseModel):
    user_id: str
    file_base64: str
    file_name: str
    file_mime_type: Optional[str] = None


def _safe_segment(value: str, label: str) -> str:
    """One path segment, no traversal. Rejects separators and dot-segments."""
    v = (value or "").strip()
    if not v or v in (".", "..") or "/" in v or "\\" in v or "\x00" in v:
        raise HTTPException(status_code=400, detail=f"invalid {label}")
    if Path(v).name != v:
        raise HTTPException(status_code=400, detail=f"invalid {label}")
    return v


def _doc_dir(user_id: str, document_id: str) -> Path:
    return KB_STORE_DIR / _safe_segment(user_id, "user_id") / _safe_segment(document_id, "document_id")


def _stored_file(user_id: str, document_id: str) -> Path:
    d = _doc_dir(user_id, document_id)
    if d.is_dir():
        for p in sorted(d.iterdir()):
            if p.is_file():
                return p
    raise HTTPException(status_code=404, detail="knowledge base file not found")


@router.post("/api/kb/upload")
async def kb_upload(req: KbUploadRequest):
    user_id = _safe_segment(req.user_id, "user_id")
    filename = Path((req.file_name or "").strip()).name
    if not filename:
        raise HTTPException(status_code=400, detail="file_name is required")

    lname = filename.lower()
    if (req.file_mime_type or "").startswith("image/") or lname.endswith(
        (".png", ".jpg", ".jpeg", ".webp", ".gif")
    ):
        raise HTTPException(
            status_code=400,
            detail="images can't be added to the Knowledge Base — attach the image directly to a message instead",
        )
    if not lname.endswith(_KB_EXTS):
        raise HTTPException(
            status_code=400,
            detail="unsupported file type — supported: PDF, Word (.docx), PowerPoint (.pptx), Excel (.xlsx/.xls), text (.txt/.md)",
        )
    if not req.file_base64:
        raise HTTPException(status_code=400, detail="the uploaded file is empty")

    try:
        raw = base64.b64decode(req.file_base64)
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="file_base64 is not valid base64")

    document_id = str(uuid.uuid4())

    try:
        data = await ingest_global_document(
            user_id=user_id, filename=filename, file_base64=req.file_base64,
            mime_type=req.file_mime_type, document_id=document_id,
        )
    except DocumentIngestError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Persist the original bytes for later preview / download.
    doc_dir = _doc_dir(user_id, data.get("document_id", document_id))
    doc_dir.mkdir(parents=True, exist_ok=True)
    (doc_dir / filename).write_bytes(raw)

    write_audit_entry(
        task_id=data.get("document_id", document_id),
        user_id=user_id,
        task_type="kb-ingest-global",
        model_used="onnx-mini-lm-l6-v2",
        file_uploaded=True,
    )

    return JSONResponse({
        "success": True,
        "document_id": data.get("document_id", document_id),
        "filename": filename,
        "file_type": Path(filename).suffix.lower().lstrip(".") or "txt",
        "status": data.get("status", "indexed"),
        "chunks": data.get("chunks", 0),
        "uploaded_at": data.get("uploaded_at"),
    })


@router.get("/api/kb/list")
async def kb_list(user_id: str = Query(...)):
    user_id = _safe_segment(user_id, "user_id")
    return {"documents": await list_global_kb_documents(user_id)}


@router.delete("/api/kb/{document_id}")
async def kb_delete(document_id: str, user_id: str = Query(...)):
    user_id = _safe_segment(user_id, "user_id")
    document_id = _safe_segment(document_id, "document_id")
    removed = await delete_global_kb_document(user_id, document_id)

    doc_dir = _doc_dir(user_id, document_id)
    if doc_dir.is_dir():
        for p in doc_dir.iterdir():
            try:
                p.unlink()
            except OSError:
                pass
        try:
            doc_dir.rmdir()
            doc_dir.parent.rmdir()  # also drop the now-empty per-user dir
        except OSError:
            pass

    return {"success": True, "document_id": document_id, "chunks_removed": removed}


@router.get("/api/kb/{document_id}/raw")
async def kb_raw(document_id: str, user_id: str = Query(...)):
    path = _stored_file(user_id, document_id)
    # inline so the preview panel's <iframe> can render a PDF directly; the
    # panel's Download button carries its own `download` attribute.
    return FileResponse(str(path), content_disposition_type="inline")


@router.get("/api/kb/{document_id}/preview")
async def kb_preview(document_id: str, user_id: str = Query(...)):
    user_id = _safe_segment(user_id, "user_id")
    document_id = _safe_segment(document_id, "document_id")
    path = _stored_file(user_id, document_id)
    rendered = render_preview(path.read_bytes(), path.name)
    rendered.update({
        "filename": path.name,
        "file_type": path.suffix.lower().lstrip(".") or "txt",
        "raw_url": f"/api/kb/{document_id}/raw?user_id={user_id}",
    })
    return rendered


@router.get("/api/preview/generated/{filename}")
async def preview_generated(filename: str):
    """Inline preview for a file this app generated into FILES_DIR (served
    at /files/<name>). `filename` must be a bare name inside FILES_DIR."""
    name = Path(filename).name
    if name != filename or not name:
        raise HTTPException(status_code=400, detail="invalid filename")
    path = (FILES_DIR / name)
    try:
        resolved = path.resolve()
        resolved.relative_to(FILES_DIR.resolve())
    except (ValueError, OSError):
        raise HTTPException(status_code=400, detail="invalid filename")
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail="file not found")

    rendered = render_preview(resolved.read_bytes(), name)
    rendered.update({
        "filename": name,
        "file_type": Path(name).suffix.lower().lstrip(".") or "txt",
        "raw_url": f"/files/{name}",
    })
    return rendered
