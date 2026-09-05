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
FILE_FORMAT_KEYWORDS = (
    "docx", "pptx", "xlsx", "excel", "spreadsheet", "powerpoint", "presentation",
    "word doc", "word document", "word file",
    "generate a doc", "generate a document", "generate a file",
    "make a document", "make a file", "create a document", "create a file",
    "as a document", "as a file",
)

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
    # Only an EXPLICIT file-format ask routes into document-generation (which
    # always ends by writing an actual file via generate_file). Content-only
    # requests like "make an approval note for the refinery" — with no file
    # format mentioned — stay text-generation and just get a text answer;
    # see model_registry.is_approval_note_request() for the separate decision
    # of whether that text answer uses the LoRA adapter.
    if any(kw in lowered for kw in FILE_FORMAT_KEYWORDS):
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
