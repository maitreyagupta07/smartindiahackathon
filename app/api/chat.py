"""
Chat-scoped Knowledge Base + conversational context (§2.3-adjacent, added
after the base contract — see PERSON_A_NOTES.md). Unchanged request/response
shapes from the pre-refactor backend/main.py; only the transport into the
tools layer changed (direct calls into app.tools.facade instead of an httpx
POST to a separate localhost:8001 Tools service).
"""
import asyncio
import uuid
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .dispatch import TASKS, TASK_LOCK, CHATS, CHAT_LOCK, MAX_HISTORY_MESSAGES, new_chat, dispatch_to_agent
from ..audit.log import write_audit_entry
from ..tools.facade import ingest_document, list_kb_documents, DocumentIngestError

router = APIRouter()


class ChatUploadRequest(BaseModel):
    file_base64: str
    file_name: str
    file_mime_type: Optional[str] = None
    chat_title: Optional[str] = None
    user_id: Optional[str] = None


class ChatMessageRequest(BaseModel):
    user_id: str
    prompt: str
    chat_title: Optional[str] = None
    # Optional per-message attachment. Used for images (jpg/png/webp) that go
    # to the vision model — dispatch_to_agent forwards these to the agent,
    # whose classifier routes any image mime type to Moondream deterministically
    # (this happens even inside a chat — see router.py's chat-vs-action logic).
    file_base64: Optional[str] = None
    file_mime_type: Optional[str] = None
    file_name: Optional[str] = None


def _valid_chat_id(chat_id: Optional[str]) -> str:
    if not chat_id or not chat_id.strip():
        raise HTTPException(status_code=400, detail="missing or invalid chat_id")
    return chat_id.strip()


@router.post("/api/chat/{chat_id}/upload")
async def chat_upload(chat_id: str, req: ChatUploadRequest):
    chat_id = _valid_chat_id(chat_id)

    filename = (req.file_name or "").strip()
    if not filename:
        raise HTTPException(status_code=400, detail="file_name is required")
    # The Knowledge Base ingests text-bearing documents. Images are not
    # indexed here — they go straight to a message as a vision attachment
    # instead (see ChatMessageRequest.file_base64 below).
    _KB_EXTS = (".pdf", ".docx", ".pptx", ".xlsx", ".xls", ".txt", ".md")
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

    document_id = str(uuid.uuid4())

    try:
        data = await ingest_document(
            chat_id=chat_id, filename=filename, file_base64=req.file_base64,
            mime_type=req.file_mime_type, document_id=document_id, chat_title=req.chat_title,
        )
    except DocumentIngestError as e:
        raise HTTPException(status_code=400, detail=str(e))

    async with CHAT_LOCK:
        chat = CHATS.setdefault(chat_id, new_chat(chat_id, req.chat_title))
        if req.chat_title:
            chat["title"] = req.chat_title
        chat["documents"] = [d for d in chat["documents"] if d.get("filename") != filename]
        chat["documents"].append({
            "document_id": data.get("document_id", document_id),
            "filename": filename,
            "chunks": data.get("chunks", 0),
            "status": data.get("status", "indexed"),
        })

    write_audit_entry(
        task_id=data.get("document_id", document_id),
        user_id=req.user_id or "unknown",
        task_type="kb-ingest",
        model_used="onnx-mini-lm-l6-v2",
        file_uploaded=True,
    )

    return JSONResponse({
        "success": True,
        "document_id": data.get("document_id", document_id),
        "filename": filename,
        "chat_id": chat_id,
        "status": data.get("status", "indexed"),
        "chunks": data.get("chunks", 0),
    })


@router.post("/api/chat/{chat_id}/message")
async def chat_message(chat_id: str, req: ChatMessageRequest, request: Request):
    chat_id = _valid_chat_id(chat_id)
    if not req.prompt or not req.prompt.strip():
        raise HTTPException(status_code=400, detail="prompt is required")

    task_id = str(uuid.uuid4())
    client_ip = request.client.host if request.client else None

    async with CHAT_LOCK:
        chat = CHATS.setdefault(chat_id, new_chat(chat_id, req.chat_title))
        if req.chat_title and not chat.get("title"):
            chat["title"] = req.chat_title
        chat["messages"].append({"role": "user", "content": req.prompt})
        history = [dict(m) for m in chat["messages"][:-1][-MAX_HISTORY_MESSAGES:]]

    async with TASK_LOCK:
        TASKS[task_id] = {
            "task_id": task_id,
            "status": "queued",
            "model_used": None,
            "started_at": None,
            "completed_at": None,
            "result": {"type": None, "text": None, "file_url": None, "file_name": None},
            "error": None,
            "models_used": None,
            "steps": None,
            "token_usage": None,
            "client_ip": client_ip,
            "_user_id": req.user_id,
            "_file_uploaded": req.file_base64 is not None,
            "_chat_id": chat_id,
        }

    asyncio.create_task(dispatch_to_agent(
        task_id=task_id, prompt=req.prompt, user_id=req.user_id,
        file_base64=req.file_base64, file_mime_type=req.file_mime_type,
        chat_id=chat_id, history=history, client_ip=client_ip,
    ))

    return JSONResponse({"task_id": task_id, "status": "queued", "chat_id": chat_id})


@router.get("/api/chat/{chat_id}")
async def get_chat(chat_id: str):
    chat_id = _valid_chat_id(chat_id)
    async with CHAT_LOCK:
        chat = CHATS.get(chat_id)
        if chat is None:
            raise HTTPException(status_code=404, detail="chat not found")
        return {
            "chat_id": chat["chat_id"],
            "title": chat["title"],
            "created_at": chat["created_at"],
            "messages": list(chat["messages"]),
            "documents": list(chat["documents"]),
        }


@router.get("/api/knowledge-base")
async def knowledge_base(chat_id: Optional[str] = None):
    documents = await list_kb_documents(chat_id)

    async with CHAT_LOCK:
        for d in documents:
            cid = d.get("chat_id")
            if cid and cid in CHATS and CHATS[cid].get("title"):
                d["chat_title"] = CHATS[cid]["title"]

    return {"documents": documents}
