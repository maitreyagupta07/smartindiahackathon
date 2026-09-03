"""
Decides task_type from the incoming request.
task_type is a short descriptive string Person F owns per contract §2.4,
e.g. "text-generation", "vision", "code-execution",
     "document-generation", "doc-search".

Also exposes needs_reasoning() — used by the planner to decide whether a
vision task should chain into a second Qwen reasoning step, per the
image + reasoning -> Moondream -> observation -> Qwen -> final pipeline.
"""

IMAGE_MIME_PREFIXES = ("image/",)

# Keywords that signal the user wants more than a raw image description —
# they want analysis/reasoning ON TOP of what the image shows.
REASONING_KEYWORDS = (
    "why", "explain", "analyze", "analyse", "summarize", "summarise",
    "findings", "insight", "reasoning", "what does this mean",
    "assess", "evaluate", "recommend", "should", "risk", "issue",
    "approval note", "report",
)


def classify_task(prompt: str, file_mime_type: str | None) -> str:
    if file_mime_type and file_mime_type.startswith(IMAGE_MIME_PREFIXES):
        return "vision"

    lowered = prompt.lower()
    if any(kw in lowered for kw in ("calculate", "compute", "run this code", "execute")):
        return "code-execution"
    if any(kw in lowered for kw in ("search", "find in docs", "sop", "manual")):
        return "doc-search"
    if any(kw in lowered for kw in ("approval note", "generate a doc", "write a report", "docx", "pptx", "xlsx")):
        return "document-generation"

    return "text-generation"


def needs_reasoning(prompt: str, file_mime_type: str | None) -> bool:
    """
    True only for image tasks where the prompt implies analysis beyond a
    plain visual description — this is what triggers the second (Qwen)
    step in the agent loop after Moondream's observation.
    """
    if not (file_mime_type and file_mime_type.startswith(IMAGE_MIME_PREFIXES)):
        return False
    lowered = prompt.lower()
    return any(kw in lowered for kw in REASONING_KEYWORDS)
