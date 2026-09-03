"""
Pydantic models mirroring the exact JSON shapes in contract §2.4.
Field names/types here are locked — do not rename or restructure
without going through the Contract Change Protocol (§4.4).
"""
from typing import Literal, Optional
from pydantic import BaseModel


class ExecuteTaskRequest(BaseModel):
    task_id: str
    prompt: str
    file_base64: Optional[str] = None
    file_mime_type: Optional[str] = None


class TaskResult(BaseModel):
    type: Literal["text", "file"]
    text: Optional[str] = None
    file_url: Optional[str] = None
    file_name: Optional[str] = None


class ExecuteTaskResponse(BaseModel):
    status: Literal["completed", "failed"]
    model_used: Optional[str] = None
    task_type: Optional[str] = None
    result: TaskResult
    error: Optional[str] = None
