"""
Real plan -> act -> observe -> replan decision logic.

decide_next_step() is called by executor/loop.py after EVERY step, not just
once — this is what makes the loop genuinely agentic rather than a fixed
pipeline: each call looks at the full state.step_records history so far
(persistent observation state) and decides what happens next.

Supported flows today (no tools yet — Person C not wired in):
  1. text-generation / code-execution / document-generation / doc-search
     (no image) -> single call_qwen step -> finalize
  2. vision, needs_reasoning == False (plain "what's in this image")
     -> single call_moondream step -> finalize
  3. vision, needs_reasoning == True (image + "explain/analyze/...")
     -> call_moondream (describe image)
        -> observe
        -> call_qwen (reason over Moondream's observation + original prompt)
        -> finalize

Extension point for Person C's tools: NextStep.action == "call_tool" is a
valid return value the loop already knows how to route (see loop.py), but
this planner never emits it yet — wire it in here once Person C's three
endpoints are ready, without touching loop.py's dispatch logic.
"""
from dataclasses import dataclass
from typing import Literal, Optional

from executor.state import TaskState
from router.model_registry import TEXT_MODEL, VISION_MODEL

Action = Literal["call_qwen", "call_moondream", "call_tool", "finalize"]


@dataclass
class NextStep:
    action: Action
    model: Optional[str] = None
    prompt: Optional[str] = None
    image_base64: Optional[str] = None


def _build_reasoning_prompt(original_prompt: str, moondream_observation: str) -> str:
    """
    Combines the user's original request with Moondream's raw image
    description into a single prompt for the Qwen reasoning step.
    """
    return (
        f"An image was analyzed and described as follows:\n"
        f"---\n{moondream_observation}\n---\n\n"
        f"Based on that description, respond to the user's original request:\n"
        f"\"{original_prompt}\""
    )


def decide_next_step(state: TaskState) -> NextStep:
    """
    Core replanning decision. Called once per loop iteration in
    executor/loop.py, AFTER observing the previous step's result.
    """
    # Max-step protection — hard stop regardless of task_type, so a bug in
    # this function's logic can never spin the loop forever.
    if state.hit_max_steps():
        print(f"[PLANNER] task_id={state.task_id} max_steps reached ({state.max_steps}) -> forcing finalize")
        return NextStep(action="finalize")

    n = state.step_count  # steps already executed so far

    # --- Step 0: nothing executed yet -> decide the entry point ---
    if n == 0:
        if state.task_type == "vision":
            print(f"[PLANNER] task_id={state.task_id} step0 -> call_moondream (vision entry point)")
            return NextStep(
                action="call_moondream",
                model=VISION_MODEL,
                prompt=state.prompt,
                image_base64=state.file_base64,
            )

        # text-generation / code-execution / document-generation / doc-search
        # all currently route to a single Qwen call — tool-calling variants
        # (execute-code, search-docs, generate-file) are the extension point
        # for Person C and are NOT implemented yet, per current scope.
        print(f"[PLANNER] task_id={state.task_id} step0 -> call_qwen (text entry point)")
        return NextStep(action="call_qwen", model=TEXT_MODEL, prompt=state.prompt)

    # --- Step 1+: replan based on what happened last ---
    last = state.step_records[-1]

    if last.status == "error":
        # Observed a failure — nothing left to retry against without tools,
        # so finalize and let the loop surface the error.
        print(f"[PLANNER] task_id={state.task_id} last step errored -> finalize")
        return NextStep(action="finalize")

    if last.action == "call_moondream":
        if state.needs_reasoning:
            # image + reasoning -> Moondream -> observation -> Qwen -> final
            reasoning_prompt = _build_reasoning_prompt(state.prompt, str(last.observation))
            print(f"[PLANNER] task_id={state.task_id} moondream observed -> chaining to call_qwen for reasoning")
            return NextStep(action="call_qwen", model=TEXT_MODEL, prompt=reasoning_prompt)
        print(f"[PLANNER] task_id={state.task_id} moondream observed, no reasoning needed -> finalize")
        return NextStep(action="finalize")

    if last.action == "call_qwen":
        # Whether this was the plain text entry point or the post-Moondream
        # reasoning step, a completed Qwen call is currently a terminal step.
        print(f"[PLANNER] task_id={state.task_id} qwen observed -> finalize")
        return NextStep(action="finalize")

    # Any other action (e.g. a future call_tool result) falls through to
    # finalize for now — extend here once Person C's tools are wired in.
    print(f"[PLANNER] task_id={state.task_id} unhandled last action '{last.action}' -> finalize")
    return NextStep(action="finalize")
