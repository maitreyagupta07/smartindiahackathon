"""
In-memory state representation for a single task's agent-loop run.
Not persisted across requests — Person B owns the audit/status store (§2.3).
This is persistent only WITHIN one /execute-task call's plan -> act ->
observe -> replan cycle, so each step can see every prior step's output.
"""
from dataclasses import dataclass, field
from typing import Any, Literal

MAX_STEPS_DEFAULT = 6  # max-step protection — hard ceiling on loop iterations


@dataclass
class StepRecord:
    """One executed step in the agent loop — persistent observation history."""
    step_number: int
    action: str                     # "call_qwen" | "call_moondream" | "call_tool" | "finalize"
    model_used: str | None
    prompt_used: str | None
    observation: Any                # raw output of this step (e.g. model response text)
    status: Literal["ok", "error"] = "ok"
    error: str | None = None


@dataclass
class TaskState:
    task_id: str
    prompt: str
    file_base64: str | None = None
    file_mime_type: str | None = None

    task_type: str | None = None
    needs_reasoning: bool = False

    # last model actually called — kept for the contract's top-level
    # `model_used` field (§2.4). For multi-model chains this is the model
    # of the FINAL step, since that's what actually produced the answer.
    model_used: str | None = None

    step_records: list[StepRecord] = field(default_factory=list)
    step_count: int = 0
    max_steps: int = MAX_STEPS_DEFAULT

    finished: bool = False
    error: str | None = None

    def add_step(
        self,
        action: str,
        model_used: str | None,
        prompt_used: str | None,
        observation: Any,
        status: str = "ok",
        error: str | None = None,
    ) -> StepRecord:
        self.step_count += 1
        record = StepRecord(
            step_number=self.step_count,
            action=action,
            model_used=model_used,
            prompt_used=prompt_used,
            observation=observation,
            status=status,
            error=error,
        )
        self.step_records.append(record)
        if model_used:
            self.model_used = model_used

        print(
            f"[STATE] task_id={self.task_id} step={record.step_number} "
            f"action={action} model={model_used} status={status}"
        )
        return record

    @property
    def last_observation(self) -> Any:
        return self.step_records[-1].observation if self.step_records else None

    def hit_max_steps(self) -> bool:
        return self.step_count >= self.max_steps
