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

# Broad, general-purpose signal for "this request wants approval-note style
# writing" — deliberately not limited to the literal phrase "approval note",
# so the LoRA adapter kicks in for realistic phrasing (e.g. "approve this
# inspection", "sign-off note for the pump") without requiring the user to
# say the exact trained phrase. Used for MODEL SELECTION only — whether the
# result becomes an actual file is a separate decision (see classifier.py).
APPROVAL_NOTE_KEYWORDS = (
    "approval note", "approval-note", "approve", "approval",
    "inspection report", "inspection finding", "inspection findings",
    "findings report", "sign-off", "sign off",
)


def get_model_for_task_type(task_type: str) -> str:
    return TASK_TYPE_TO_MODEL.get(task_type, TEXT_MODEL)


def is_approval_note_request(prompt: str) -> bool:
    lowered = prompt.lower()
    return any(kw in lowered for kw in APPROVAL_NOTE_KEYWORDS)
