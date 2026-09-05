"""
PERSON B — API, Router & Task Queue
Implements §2.3 (Frontend <-> Backend), dispatches per §2.4 (Backend <-> Agent Loop),
serves the frontend + /files/ per §2.7 / §2.7a, and writes a hash-chained audit log.

Does NOT implement any model or tool logic — that is Person F's responsibility.
This service only receives, queues, tracks, and responds.
"""
import asyncio
import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Config — read from shared config.json, never hardcode (self-check §2.10.4)
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config.json"
with open(CONFIG_PATH) as f:
    CONFIG = json.load(f)

BACKEND_PORT = CONFIG["ports"]["backend"]
EXECUTOR_PORT = CONFIG["ports"]["agent_executor"]
EXECUTOR_URL = f"http://localhost:{EXECUTOR_PORT}/execute-task"
# Person C's Tools service — localhost only (§2.2). The chat-scoped
# Knowledge Base ingestion/listing endpoints live there (it owns the vector
# DB + embedding pipeline); this service only proxies to them.
TOOLS_PORT = CONFIG["ports"].get("tools", 8001)
TOOLS_BASE_URL = f"http://localhost:{TOOLS_PORT}"
# Anchor to REPO_ROOT rather than resolving "./shared_files" against the
# process's cwd — this README documents launching from *inside* backend/,
# which would otherwise silently resolve to backend/shared_files, a
# different physical directory than the one Person C's tools service (see
# tools/app/config.py, which anchors the same way) actually writes
# generated files into. That mismatch would 404 every /files/<name> link.
_raw_files_dir = Path(CONFIG["FILES_DIR"])
FILES_DIR = _raw_files_dir if _raw_files_dir.is_absolute() else (REPO_ROOT / _raw_files_dir).resolve()
FILES_DIR.mkdir(parents=True, exist_ok=True)
MAX_CONCURRENT_TASKS = CONFIG.get("backend", {}).get("max_concurrent_tasks", 2)

DB_PATH = Path(__file__).resolve().parent / "audit_log.sqlite3"
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"  # Person D1 drops build here

app = FastAPI(title="Person B - API, Router & Task Queue")

# In-memory task store: task_id -> dict matching §2.3 GET /api/task-status shape
TASKS: dict[str, dict] = {}
TASK_LOCK = asyncio.Lock()

# In-memory chat store: chat_id -> {chat_id, title, created_at, messages[], documents[]}
# Deliberately in-memory only, mirroring TASKS above — the goal is conversational
# continuity within an active chat, NOT permanent chat-history storage, so no
# messages table is introduced. The uploaded documents themselves ARE persisted:
# their chunks/embeddings live on disk in the Tools service's ChromaDB, and the
# Admin -> Knowledge Base listing is derived from there, so it survives a restart
# of this service even though the in-memory conversation context does not.
CHATS: dict[str, dict] = {}
CHAT_LOCK = asyncio.Lock()

# How many trailing conversation messages to send to the agent as context.
# ~5 turns — enough for follow-up references ("it", "they") without shipping an
# unbounded transcript on every question.
MAX_HISTORY_MESSAGES = 10


def _new_chat(chat_id: str, title: Optional[str] = None) -> dict:
    return {
        "chat_id": chat_id,
        "title": title,
        "created_at": now_iso(),
        "messages": [],   # [{"role": "user"|"assistant", "content": str}]
        "documents": [],  # [{document_id, filename, chunks, status}]
    }

# Concurrency cap — a semaphore, not a serializing queue: extra tasks stay
# "queued" until a slot frees, but slots run genuinely in parallel (§2.4 critical rule).
EXECUTION_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_TASKS)


# ---------------------------------------------------------------------------
# Request/response models — field names/types copied verbatim from §2.3/§2.4
# ---------------------------------------------------------------------------
class SubmitTaskRequest(BaseModel):
    user_id: str
    prompt: str
    file_base64: Optional[str] = None
    file_name: Optional[str] = None
    file_mime_type: Optional[str] = None


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Audit log — SQLite, hash-chained (§4.1 diagram: AuditLog)
# ---------------------------------------------------------------------------
import sqlite3


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            task_type TEXT NOT NULL,
            model_used TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            file_uploaded INTEGER NOT NULL,
            prev_hash TEXT NOT NULL,
            entry_hash TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


def _last_hash(conn) -> str:
    row = conn.execute(
        "SELECT entry_hash FROM audit_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return row[0] if row else "0" * 64


def write_audit_entry(task_id: str, user_id: str, task_type: str, model_used: str,
                       file_uploaded: bool):
    conn = sqlite3.connect(DB_PATH)
    prev_hash = _last_hash(conn)
    timestamp = now_iso()
    payload = f"{task_id}|{user_id}|{task_type}|{model_used}|{timestamp}|{file_uploaded}|{prev_hash}"
    entry_hash = hashlib.sha256(payload.encode()).hexdigest()
    conn.execute(
        """INSERT INTO audit_log
           (task_id, user_id, task_type, model_used, timestamp, file_uploaded, prev_hash, entry_hash)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (task_id, user_id, task_type, model_used, timestamp, int(file_uploaded), prev_hash, entry_hash),
    )
    conn.commit()
    conn.close()


def read_audit_entries() -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT task_id, user_id, task_type, model_used, timestamp, file_uploaded FROM audit_log ORDER BY id ASC"
    ).fetchall()
    conn.close()
    return [
        {
            "task_id": r[0],
            "user_id": r[1],
            "task_type": r[2],
            "model_used": r[3],
            "timestamp": r[4],
            "file_uploaded": bool(r[5]),
        }
        for r in rows
    ]


init_db()


# ---------------------------------------------------------------------------
# POST /api/submit-task  (§2.3)
# ---------------------------------------------------------------------------
@app.post("/api/submit-task")
async def submit_task(req: SubmitTaskRequest):
    task_id = str(uuid.uuid4())
    file_uploaded = req.file_base64 is not None

    async with TASK_LOCK:
        TASKS[task_id] = {
            "task_id": task_id,
            "status": "queued",
            "model_used": None,
            "started_at": None,
            "completed_at": None,
            "result": {"type": None, "text": None, "file_url": None, "file_name": None},
            "error": None,
            "_user_id": req.user_id,
            "_file_uploaded": file_uploaded,
        }

    # Fire-and-forget dispatch — do NOT await this before responding (§2.4 critical rule).
    asyncio.create_task(_dispatch_to_executor(task_id, req))

    return JSONResponse({"task_id": task_id, "status": "queued"})


async def _dispatch_to_executor(
    task_id: str,
    req,
    chat_id: Optional[str] = None,
    history: Optional[list] = None,
):
    file_base64 = getattr(req, "file_base64", None)
    file_mime_type = getattr(req, "file_mime_type", None)

    async with EXECUTION_SEMAPHORE:
        async with TASK_LOCK:
            TASKS[task_id]["status"] = "processing"
            TASKS[task_id]["started_at"] = now_iso()

        executor_payload = {
            "task_id": task_id,
            "prompt": req.prompt,
            "file_base64": file_base64,
            "file_mime_type": file_mime_type,
        }
        if chat_id:
            # Additive fields — the agent's ExecuteTaskRequest accepts them as
            # optional; a mock/older executor simply ignores them.
            executor_payload["chat_id"] = chat_id
            executor_payload["history"] = history or []

        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.post(EXECUTOR_URL, json=executor_payload)
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            async with TASK_LOCK:
                TASKS[task_id]["status"] = "failed"
                TASKS[task_id]["completed_at"] = now_iso()
                TASKS[task_id]["error"] = f"Executor unreachable or errored: {e}"
            write_audit_entry(
                task_id=task_id,
                user_id=req.user_id,
                task_type="chat" if chat_id else "unknown",
                model_used="none",
                file_uploaded=file_base64 is not None,
            )
            return

        async with TASK_LOCK:
            TASKS[task_id]["status"] = data.get("status", "failed")
            TASKS[task_id]["completed_at"] = now_iso()
            TASKS[task_id]["model_used"] = data.get("model_used")
            TASKS[task_id]["result"] = data.get("result") or {
                "type": None, "text": None, "file_url": None, "file_name": None
            }
            TASKS[task_id]["error"] = data.get("error")

        # Chat flow: record the assistant's answer as the next conversation
        # turn so the following question carries it as context.
        if chat_id and data.get("status") == "completed":
            answer = (data.get("result") or {}).get("text")
            if answer:
                async with CHAT_LOCK:
                    chat = CHATS.get(chat_id)
                    if chat is not None:
                        chat["messages"].append({"role": "assistant", "content": answer})

        write_audit_entry(
            task_id=task_id,
            user_id=req.user_id,
            task_type=data.get("task_type") or ("chat" if chat_id else "unknown"),
            model_used=data.get("model_used") or "none",
            file_uploaded=file_base64 is not None,
        )


# ---------------------------------------------------------------------------
# GET /api/task-status/{task_id}  (§2.3)
# ---------------------------------------------------------------------------
@app.get("/api/task-status/{task_id}")
async def task_status(task_id: str):
    async with TASK_LOCK:
        task = TASKS.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        return {
            "task_id": task["task_id"],
            "status": task["status"],
            "model_used": task["model_used"],
            "started_at": task["started_at"],
            "completed_at": task["completed_at"],
            "result": task["result"],
            "error": task["error"],
        }
    

# ---------------------------------------------------------------------------
# GET /api/audit-log  (§2.3)
# ---------------------------------------------------------------------------
@app.get("/api/audit-log")
async def audit_log():
    return {"entries": read_audit_entries()}


# ===========================================================================
# Chat-scoped Knowledge Base + conversational context
# ===========================================================================
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


def _valid_chat_id(chat_id: Optional[str]) -> str:
    """Every chat-scoped call needs a usable chat_id — reject missing/blank."""
    if not chat_id or not chat_id.strip():
        raise HTTPException(status_code=400, detail="missing or invalid chat_id")
    return chat_id.strip()


# ---------------------------------------------------------------------------
# POST /api/chat/{chat_id}/upload
# Ingest a PDF into THIS chat's Knowledge Base (chunks + embeddings persisted
# in the Tools service's ChromaDB, tagged with chat_id). The chat upload IS
# the ingestion action — the file then appears in Admin -> Knowledge Base.
# ---------------------------------------------------------------------------
@app.post("/api/chat/{chat_id}/upload")
async def chat_upload(chat_id: str, req: ChatUploadRequest):
    chat_id = _valid_chat_id(chat_id)

    filename = (req.file_name or "").strip()
    if not filename:
        raise HTTPException(status_code=400, detail="file_name is required")
    is_pdf = (req.file_mime_type == "application/pdf") or filename.lower().endswith(".pdf")
    if not is_pdf:
        raise HTTPException(
            status_code=400,
            detail="unsupported file type — only PDF uploads are supported",
        )
    if not req.file_base64:
        raise HTTPException(status_code=400, detail="the uploaded file is empty")

    document_id = str(uuid.uuid4())
    payload = {
        "chat_id": chat_id,
        "document_id": document_id,
        "filename": filename,
        "file_base64": req.file_base64,
        "mime_type": req.file_mime_type,
        "chat_title": req.chat_title,
    }

    try:
        async with httpx.AsyncClient(timeout=180.0) as client:
            resp = await client.post(f"{TOOLS_BASE_URL}/tools/ingest-doc", json=payload)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"knowledge-base service unreachable: {e}")

    if resp.status_code != 200:
        try:
            detail = resp.json().get("error") or resp.text
        except Exception:
            detail = resp.text or "document ingestion failed"
        # Surface the Tools service's own 4xx (bad/empty/corrupt PDF) as-is;
        # anything else becomes a 502.
        status = resp.status_code if 400 <= resp.status_code < 500 else 502
        raise HTTPException(status_code=status, detail=detail)

    data = resp.json()

    async with CHAT_LOCK:
        chat = CHATS.setdefault(chat_id, _new_chat(chat_id, req.chat_title))
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


# ---------------------------------------------------------------------------
# POST /api/chat/{chat_id}/message
# Ask a question in a chat. Appends the user turn, sends recent conversation
# history + chat_id to the agent (which retrieves this chat's KB and answers),
# then records the assistant turn. Unlimited follow-ups — the PDF is indexed
# once and reused for every subsequent question.
# ---------------------------------------------------------------------------
@app.post("/api/chat/{chat_id}/message")
async def chat_message(chat_id: str, req: ChatMessageRequest):
    chat_id = _valid_chat_id(chat_id)
    if not req.prompt or not req.prompt.strip():
        raise HTTPException(status_code=400, detail="prompt is required")

    task_id = str(uuid.uuid4())

    async with CHAT_LOCK:
        chat = CHATS.setdefault(chat_id, _new_chat(chat_id, req.chat_title))
        if req.chat_title and not chat.get("title"):
            chat["title"] = req.chat_title
        chat["messages"].append({"role": "user", "content": req.prompt})
        # Context = the turns BEFORE this new question, capped.
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
            "_user_id": req.user_id,
            "_file_uploaded": False,
            "_chat_id": chat_id,
        }

    asyncio.create_task(_dispatch_to_executor(task_id, req, chat_id=chat_id, history=history))

    return JSONResponse({"task_id": task_id, "status": "queued", "chat_id": chat_id})


# ---------------------------------------------------------------------------
# GET /api/chat/{chat_id}  — resume a chat (messages + its documents)
# ---------------------------------------------------------------------------
@app.get("/api/chat/{chat_id}")
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


# ---------------------------------------------------------------------------
# GET /api/knowledge-base  — Admin -> Knowledge Base listing (all chats, or
# ?chat_id=... for one). Proxies the Tools service, which derives the list
# from the ChromaDB chunk metadata (no separate documents table).
# ---------------------------------------------------------------------------
@app.get("/api/knowledge-base")
async def knowledge_base(chat_id: Optional[str] = None):
    params = {"chat_id": chat_id} if chat_id else None
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(f"{TOOLS_BASE_URL}/tools/kb-documents", params=params)
            resp.raise_for_status()
            documents = resp.json().get("documents", [])
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"knowledge-base service unreachable: {e}")

    # Fill in a friendly chat title from live chat state where the vector
    # store only had a chat_id (e.g. chat was renamed after upload).
    async with CHAT_LOCK:
        for d in documents:
            cid = d.get("chat_id")
            if cid and cid in CHATS and CHATS[cid].get("title"):
                d["chat_title"] = CHATS[cid]["title"]

    return {"documents": documents}


# ---------------------------------------------------------------------------
# Error handling convention (§2.8) — flat {"error": "..."} for request/service
# level problems, not task failures.
# ---------------------------------------------------------------------------
@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"error": str(exc.detail)})


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc: RequestValidationError):
    # Malformed/missing-field request bodies must return the §2.8 flat shape
    # with 400, not FastAPI's default 422 validation-error blob.
    return JSONResponse(status_code=400, content={"error": f"invalid request body: {exc.errors()}"})


@app.exception_handler(Exception)
async def generic_exception_handler(request, exc: Exception):
    return JSONResponse(status_code=500, content={"error": f"internal error: {exc}"})


# ---------------------------------------------------------------------------
# Static serving (§2.7) and generated-file serving (§2.7a)
# ---------------------------------------------------------------------------
app.mount("/files", StaticFiles(directory=str(FILES_DIR)), name="files")

if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn
    # bind 0.0.0.0 per Person E's build step 1 — must be reachable from the LAN, not just localhost
    uvicorn.run(app, host="0.0.0.0", port=BACKEND_PORT)
