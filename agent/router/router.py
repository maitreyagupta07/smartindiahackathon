"""
Top-level routing decision: given a task, pick which model to use.
Per §2.5 locked-in model names:
  - text/code: qwen2.5:1.5b-instruct
  - vision:    moondream
"""
from router.classifier import classify_task
from router.model_registry import get_model_for_task_type
from typing import Optional

async def route_task(prompt: str, file_mime_type: Optional[str]) -> tuple[str, str]:
    """
    Returns (task_type, model_name).
    """
    task_type = classify_task(prompt, file_mime_type)
    model_name = get_model_for_task_type(task_type)
    return task_type, model_name

