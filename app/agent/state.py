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
    observation: Any                # raw output of this step (model text, or tool result dict)
    status: Literal["ok", "error"] = "ok"
    error: str | None = None
    tool_name: str | None = None    # "execute_code" | "search_docs" | "generate_file" (call_tool only)
    tool_args: dict | None = None   # exact args passed to the tool (call_tool only) — lets a
                                     # retry or later replan step reuse them verbatim instead of
                                     # re-deriving (e.g. re-running content-prep) from scratch.
    # Real token counts straight from Ollama's own response (prompt_eval_count/
    # eval_count — see app/inference/client.py) for a call_qwen/call_moondream
    # step. None for call_tool/finalize steps, or if Ollama didn't report them.
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


@dataclass
class TaskState:
    task_id: str
    prompt: str
    file_base64: str | None = None
    file_mime_type: str | None = None

    task_type: str | None = None
    needs_reasoning: bool = False

    # Chat-flow inputs (set only when the request carried a chat_id).
    # history: recent [{"role": "user"|"assistant", "content": str}, ...]
    # sources: filled in by the planner from the chat-scoped KB retrieval so
    #          the final response can cite filename/page.
    chat_id: str | None = None
    history: list | None = None
    sources: list | None = None

    # last model actually called — kept for the contract's top-level
    # `model_used` field (§2.4). For multi-model chains this is the model
    # of the FINAL step, since that's what actually produced the answer.
    model_used: str | None = None

    step_records: list[StepRecord] = field(default_factory=list)
    step_count: int = 0
    max_steps: int = MAX_STEPS_DEFAULT

    finished: bool = False
    error: str | None = None

    # File-generation-specific persistent state — set as the document-generation
    # flow progresses so later steps (and the final response) can see what was
    # verified/prepared without re-deriving it from step_records.
    prepared_file_content: dict | None = None  # structured {"title","sections"} built for generate_file
    file_type: str | None = None               # "docx" | "xlsx" | "pptx" once determined
    file_url: str | None = None
    file_name: str | None = None

    def add_step(
        self,
        action: str,
        model_used: str | None,
        prompt_used: str | None,
        observation: Any,
        status: str = "ok",
        error: str | None = None,
        tool_name: str | None = None,
        tool_args: dict | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
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
            tool_name=tool_name,
            tool_args=tool_args,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
        self.step_records.append(record)
        if model_used:
            self.model_used = model_used

        print(
            f"[STATE] task_id={self.task_id} step={record.step_number} "
            f"action={action} tool={tool_name} model={model_used} status={status}"
        )
        return record

    @property
    def last_observation(self) -> Any:
        return self.step_records[-1].observation if self.step_records else None

    def hit_max_steps(self) -> bool:
        return self.step_count >= self.max_steps

    @property
    def models_used(self) -> list[str]:
        """
        Every distinct model actually invoked during this run, in the order
        first used — e.g. ["moondream", "qwen2.5:1.5b-instruct"] for an
        image+reasoning task. `model_used` (singular) only ever kept the
        LAST model for the contract's top-level field; this is additive, for
        surfacing the real multi-model chain to the frontend's activity map
        without touching that existing field.
        """
        seen: list[str] = []
        for record in self.step_records:
            if record.model_used and record.model_used not in seen:
                seen.append(record.model_used)
        return seen

    @property
    def step_summary(self) -> list[dict]:
        """
        A lightweight, frontend-safe trace of every step actually executed —
        action/model/tool/status only, never the raw prompt text or full
        observation (avoids bloating the response or leaking prompt
        internals). This is what lets the activity map show what genuinely
        happened instead of an inferred/honest-but-vague guess.
        """
        return [
            {
                "step_number": r.step_number,
                "action": r.action,
                "model_used": r.model_used,
                "tool_name": r.tool_name,
                "status": r.status,
                "prompt_tokens": r.prompt_tokens,
                "completion_tokens": r.completion_tokens,
            }
            for r in self.step_records
        ]

    @property
    def token_totals(self) -> dict:
        """
        Real token counts summed across every model call in this task —
        straight from Ollama's own prompt_eval_count/eval_count fields, never
        estimated. {"prompt_tokens": int, "completion_tokens": int,
        "total_tokens": int}; all zero if Ollama never reported any (e.g. a
        pure tool/doc-search task with no model call, or an older Ollama
        version that omits these fields).
        """
        prompt = sum(r.prompt_tokens or 0 for r in self.step_records)
        completion = sum(r.completion_tokens or 0 for r in self.step_records)
        return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}
