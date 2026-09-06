"""
Pydantic models mirroring the exact JSON shapes in contract §2.4.
Field names/types here are locked — do not rename or restructure
without going through the Contract Change Protocol (§4.4).
"""
from typing import Any, Literal, Optional
from pydantic import BaseModel


class ExecuteTaskRequest(BaseModel):
    task_id: str
    prompt: str
    file_base64: Optional[str] = None
    file_mime_type: Optional[str] = None
    # Optional, additive (default None keeps every existing caller/shape
    # identical). When `chat_id` is set the loop runs the chat flow:
    # chat-scoped Knowledge Base retrieval + `history` (recent conversation
    # turns) folded into the Qwen prompt so follow-up questions resolve
    # references like "it"/"they" against what was said earlier.
    chat_id: Optional[str] = None
    history: Optional[list[dict]] = None


class TaskResult(BaseModel):
    type: Literal["text", "file"]
    text: Optional[str] = None
    file_url: Optional[str] = None
    file_name: Optional[str] = None
    # Populated only for the chat flow — [{filename, page, score}, ...] for
    # the KB passages the answer was grounded in. Optional/defaulted so the
    # §2.4 response shape is unchanged for every other flow.
    sources: Optional[list[dict]] = None


class ExecuteTaskResponse(BaseModel):
    status: Literal["completed", "failed"]
    model_used: Optional[str] = None
    task_type: Optional[str] = None
    result: TaskResult
    error: Optional[str] = None
