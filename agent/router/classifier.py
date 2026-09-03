"""
Decides task_type from the incoming request.
task_type is a short descriptive string Person F owns per contract §2.4,
e.g. "text-generation", "vision", "code-execution",
     "document-generation", "doc-search".
"""

IMAGE_MIME_PREFIXES = ("image/",)

from typing import Optional

def classify_task(prompt: str, file_mime_type: Optional[str]) -> str:
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
