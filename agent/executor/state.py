"""
In-memory state representation for a single task's agent-loop run.
Not persisted — Person B owns the audit/status store (§2.3).
"""
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class TaskState:
    task_id: str
    prompt: str
    file_base64: Optional[str] = None
    file_mime_type: Optional[str] = None

    task_type: Optional[str] = None
    model_used: Optional[str] = None

    steps: list[dict[str, Any]] = field(default_factory=list)
    finished: bool = False
    error: Optional[str] = None

    def add_step(self, action: str, observation: Any) -> None:
        self.steps.append({"action": action, "observation": observation})
