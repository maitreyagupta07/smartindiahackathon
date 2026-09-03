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
CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"
with open(CONFIG_PATH) as f:
    CONFIG = json.load(f)

BACKEND_PORT = CONFIG["ports"]["backend"]
EXECUTOR_PORT = CONFIG["ports"]["agent_executor"]
EXECUTOR_URL = f"http://localhost:{EXECUTOR_PORT}/execute-task"
FILES_DIR = Path(CONFIG["FILES_DIR"]).resolve()
FILES_DIR.mkdir(parents=True, exist_ok=True)
MAX_CONCURRENT_TASKS = CONFIG.get("backend", {}).get("max_concurrent_tasks", 2)

DB_PATH = Path(__file__).resolve().parent / "audit_log.sqlite3"
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"  # Person D1 drops build here

app = FastAPI(title="Person B - API, Router & Task Queue")

# In-memory task store: task_id -> dict matching §2.3 GET /api/task-status shape
TASKS: dict[str, dict] = {}
TASK_LOCK = asyncio.Lock()

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


async def _dispatch_to_executor(task_id: str, req: SubmitTaskRequest):
    async with EXECUTION_SEMAPHORE:
        async with TASK_LOCK:
            TASKS[task_id]["status"] = "processing"
            TASKS[task_id]["started_at"] = now_iso()

        executor_payload = {
            "task_id": task_id,
            "prompt": req.prompt,
            "file_base64": req.file_base64,
            "file_mime_type": req.file_mime_type,
        }

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
                task_type="unknown",
                model_used="none",
                file_uploaded=req.file_base64 is not None,
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

        write_audit_entry(
            task_id=task_id,
            user_id=req.user_id,
            task_type=data.get("task_type") or "unknown",
            model_used=data.get("model_used") or "none",
            file_uploaded=req.file_base64 is not None,
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
