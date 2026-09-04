"""
Single source of truth for model name strings.
Per contract §2.5 — do not hardcode these anywhere else in /agent.
If Person A changes a model name, only this file should need updating.
"""

TEXT_MODEL = "qwen2.5:1.5b-instruct"
VISION_MODEL = "moondream"
LORA_ADAPTER = "approval-note-lora"  # Person A's fine-tuned adapter for approval-note file generation

TASK_TYPE_TO_MODEL = {
    "vision": VISION_MODEL,
    "text-generation": TEXT_MODEL,
    "code-execution": TEXT_MODEL,
    "document-generation": TEXT_MODEL,
    "doc-search": TEXT_MODEL,
}


def get_model_for_task_type(task_type: str) -> str:
    return TASK_TYPE_TO_MODEL.get(task_type, TEXT_MODEL)
