"""
Top-level routing decision: given a task, pick which model to use.
Per §2.5 locked-in model names:
  - text/code: qwen2.5:1.5b-instruct
  - vision:    moondream
"""
from router.classifier import classify_task, needs_reasoning
from router.model_registry import get_model_for_task_type


async def route_task(prompt: str, file_mime_type: str | None) -> tuple[str, str, bool]:
    """
    Returns (task_type, model_name, needs_reasoning_flag).

    model_name here is the model for the FIRST step only. For
    task_type == "vision" with needs_reasoning_flag == True, the planner
    (executor/planner.py) is responsible for chaining a second Qwen step
    after Moondream's observation — routing only decides the entry point.
    """
    task_type = classify_task(prompt, file_mime_type)
    model_name = get_model_for_task_type(task_type)
    reasoning_flag = needs_reasoning(prompt, file_mime_type)

    print(f"[ROUTER] task_type={task_type} first_model={model_name} needs_reasoning={reasoning_flag}")

    return task_type, model_name, reasoning_flag
