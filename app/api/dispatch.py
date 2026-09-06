"""
Shared task/chat in-memory state + the single dispatch path into the agent
layer. Used by both api/tasks.py (plain one-shot tasks) and api/chat.py
(chat-scoped tasks) — centralized here so both can share the same TASKS
store, chat store, and concurrency semaphores without importing each other.

Before the single-node refactor, dispatch meant an httpx POST to a separate
agent process on localhost:8002. Now it's a direct in-process call to
app.agent.loop.run_agent_loop — same ExecuteTaskRequest/ExecuteTaskResponse
shapes, same fire-and-forget asyncio.create_task pattern, same semaphore-
gated concurrency. No contract changed; only the transport between API and
agent did (function call instead of HTTP).
"""
import asyncio
from typing import Optional

from ..agent.loop import run_agent_loop
from ..audit.log import now_iso, write_audit_entry
from ..schemas.task import ExecuteTaskRequest
from ..storage.config import MAX_CONCURRENT_TASKS, MAX_CONCURRENT_VISION_TASKS

# In-memory task store: task_id -> dict matching §2.3 GET /api/task-status shape
TASKS: dict[str, dict] = {}
TASK_LOCK = asyncio.Lock()

# In-memory chat store: chat_id -> {chat_id, title, created_at, messages[], documents[]}
# Deliberately in-memory only — conversational continuity within an active
# chat, NOT permanent chat-history storage. The uploaded documents' chunks/
# embeddings live on disk in ChromaDB (app/tools/docsearch.py), so the
# Knowledge Base listing survives a restart even though chat messages don't.
CHATS: dict[str, dict] = {}
CHAT_LOCK = asyncio.Lock()

# How many trailing conversation messages to send to the agent as context.
MAX_HISTORY_MESSAGES = 10

# Two concurrency lanes, not one: plain text/document/code/search tasks share
# the general semaphore, but vision tasks (a Moondream image inference) are
# heavier on a single mid-range GPU and get their own, smaller lane — so a
# burst of vision requests queues safely instead of starving/competing with
# ordinary text tasks for the same slots, without blocking either kind
# outright (still a semaphore, not a serializing queue — §2.4's "must
# genuinely overlap" rule holds within each lane).
EXECUTION_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_TASKS)
VISION_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_VISION_TASKS)


def new_chat(chat_id: str, title: Optional[str] = None) -> dict:
    return {
        "chat_id": chat_id,
        "title": title,
        "created_at": now_iso(),
        "messages": [],
        "documents": [],
    }


def _is_vision_request(file_mime_type: Optional[str]) -> bool:
    return bool(file_mime_type) and file_mime_type.startswith("image/")


async def dispatch_to_agent(
    task_id: str,
    prompt: str,
    user_id: str,
    file_base64: Optional[str] = None,
    file_mime_type: Optional[str] = None,
    chat_id: Optional[str] = None,
    history: Optional[list] = None,
):
    """
    Runs one task through the agent loop in the background (fire-and-forget
    from the caller's perspective — the caller has already returned
    {"task_id","status":"queued"} to the browser before this coroutine is
    scheduled). Picks the vision lane when an image was uploaded, otherwise
    the general lane — decided here, cheaply, by mime type alone, without
    duplicating the router's full classification logic.
    """
    semaphore = VISION_SEMAPHORE if _is_vision_request(file_mime_type) else EXECUTION_SEMAPHORE

    async with semaphore:
        async with TASK_LOCK:
            TASKS[task_id]["status"] = "processing"
            TASKS[task_id]["started_at"] = now_iso()

        try:
            req = ExecuteTaskRequest(
                task_id=task_id,
                prompt=prompt,
                file_base64=file_base64,
                file_mime_type=file_mime_type,
                chat_id=chat_id,
                history=history,
            )
            resp = await run_agent_loop(req)
            data = resp.model_dump()
        except Exception as e:  # noqa: BLE001 — dispatch itself must never crash silently
            async with TASK_LOCK:
                TASKS[task_id]["status"] = "failed"
                TASKS[task_id]["completed_at"] = now_iso()
                TASKS[task_id]["error"] = f"Agent execution failed: {e}"
            write_audit_entry(
                task_id=task_id, user_id=user_id,
                task_type="chat" if chat_id else "unknown",
                model_used="none", file_uploaded=file_base64 is not None,
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

        if chat_id and data.get("status") == "completed":
            answer = (data.get("result") or {}).get("text")
            if answer:
                async with CHAT_LOCK:
                    chat = CHATS.get(chat_id)
                    if chat is not None:
                        chat["messages"].append({"role": "assistant", "content": answer})

        write_audit_entry(
            task_id=task_id, user_id=user_id,
            task_type=data.get("task_type") or ("chat" if chat_id else "unknown"),
            model_used=data.get("model_used") or "none",
            file_uploaded=file_base64 is not None,
        )
