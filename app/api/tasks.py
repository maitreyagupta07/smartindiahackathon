"""
POST /api/submit-task, GET /api/task-status/{task_id}, GET /api/audit-log
(contract §2.3) — unchanged request/response shapes from the pre-refactor
backend/main.py.
"""
import asyncio
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .auth import current_admin
from .dispatch import TASKS, TASK_LOCK, dispatch_to_agent
from ..audit.log import read_audit_entries

router = APIRouter()


class SubmitTaskRequest(BaseModel):
    user_id: str
    prompt: str
    file_base64: Optional[str] = None
    file_name: Optional[str] = None
    file_mime_type: Optional[str] = None


@router.post("/api/submit-task")
async def submit_task(req: SubmitTaskRequest, request: Request):
    task_id = str(uuid.uuid4())
    file_uploaded = req.file_base64 is not None
    # The real, actual IP FastAPI/Starlette saw this request come from — not
    # anything the client claims about itself. This is what makes "which
    # employee/machine is this" trustworthy for the admin view: it's a fact
    # about the TCP connection, not a value the browser sent us.
    client_ip = request.client.host if request.client else None

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
            "_file_uploaded": file_uploaded,
        }

    # Fire-and-forget dispatch — do NOT await this before responding (§2.4 critical rule).
    asyncio.create_task(dispatch_to_agent(
        task_id=task_id,
        prompt=req.prompt,
        user_id=req.user_id,
        file_base64=req.file_base64,
        file_mime_type=req.file_mime_type,
        client_ip=client_ip,
    ))

    return JSONResponse({"task_id": task_id, "status": "queued"})


@router.get("/api/task-status/{task_id}")
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
            "models_used": task.get("models_used"),
            "steps": task.get("steps"),
            "token_usage": task.get("token_usage"),
            "client_ip": task.get("client_ip"),
        }


@router.get("/api/audit-log")
async def audit_log(_admin: str = Depends(current_admin)):
    """Admin-only. This is the whole activity record for every operator —
    user ids, client IPs, timestamps, token counts. It was previously served
    to any unauthenticated caller on the LAN while only the Admin *UI* was
    passcode-gated, so the data behind that gate was open."""
    return {"entries": read_audit_entries()}
