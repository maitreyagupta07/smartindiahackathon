"""
Pydantic models mirroring the exact JSON shapes in contract §2.4.
Field names/types here are locked — do not rename or restructure
without going through the Contract Change Protocol (§4.4).
"""
from typing import Any, Literal, Optional
from pydantic import BaseModel, ConfigDict


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
    # `model_used` is a real contract field name (§2.4) — protected_namespaces=()
    # just tells pydantic not to warn that it looks like one of pydantic's own
    # reserved `model_*` methods. Purely cosmetic: no JSON shape change.
    model_config = ConfigDict(protected_namespaces=())

    status: Literal["completed", "failed"]
    model_used: Optional[str] = None
    task_type: Optional[str] = None
    result: TaskResult
    error: Optional[str] = None
    # Additive, optional — every existing caller that only reads model_used/
    # task_type/result/error is completely unaffected (defaults to None).
    # models_used: every distinct model actually invoked, in call order —
    # e.g. ["moondream", "qwen2.5:1.5b-instruct"] for an image+reasoning
    # task — for surfacing the real multi-model chain (model_used alone
    # only ever kept the LAST one). steps: a lightweight, prompt-free trace
    # of each executed step (action/model/tool/status) for the frontend's
    # activity map to render what genuinely happened instead of an inferred
    # guess. See app/agent/state.py's models_used/step_summary properties.
    models_used: Optional[list[str]] = None
    steps: Optional[list[dict]] = None
