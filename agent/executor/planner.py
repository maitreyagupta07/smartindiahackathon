"""
Real plan -> act -> observe -> replan decision logic.

decide_next_step() is called by executor/loop.py after EVERY step, not just
once — this is what makes the loop genuinely agentic rather than a fixed
pipeline: each call looks at the full state.step_records history so far
(persistent observation state) and decides what happens next.

Supported flows:
  1. text-generation
     -> call_qwen -> finalize

  2. vision, needs_reasoning == False (plain "what's in this image")
     -> call_moondream -> finalize

  3. vision, needs_reasoning == True (image + "explain/analyze/...")
     -> call_moondream (describe image)
        -> observe
        -> call_qwen (reason over Moondream's observation + original prompt)
        -> finalize

  4. code-execution
     -> call_tool(execute_code)
        -> observe stdout/stderr/exit_code
        -> call_qwen (explain/verify the result against the original ask)
        -> finalize

  5. doc-search
     -> call_tool(search_docs)
        -> observe matched passages
        -> call_qwen (answer the original prompt grounded in those passages)
        -> finalize

  6. document-generation
     -> call_tool(generate_file)
        -> observe file_url/file_name
        -> finalize   (no Qwen step — Person C's generator is the actual
                        deliverable; Qwen must not merely describe the file)

Tool dispatch uses Person C's existing endpoints exactly as contracted in
§2.6, via clients/tools_client.py — no duplicate tool logic lives here,
only the decision of WHEN to call which tool and what to pass it.
"""
from dataclasses import dataclass
from typing import Literal, Optional

from executor.state import TaskState
from router.model_registry import TEXT_MODEL, VISION_MODEL

Action = Literal["call_qwen", "call_moondream", "call_tool", "finalize"]
ToolName = Literal["execute_code", "search_docs", "generate_file"]

# A tool call is allowed at most this many total attempts (initial + retries)
# before the planner gives up and finalizes with an error. Independent of,
# and tighter than, state.max_steps — this bounds retries specifically.
MAX_TOOL_ATTEMPTS = 2


@dataclass
class NextStep:
    action: Action
    model: Optional[str] = None
    prompt: Optional[str] = None
    image_base64: Optional[str] = None
    tool_name: Optional[ToolName] = None
    tool_args: Optional[dict] = None


# ---------------------------------------------------------------------------
# Helpers: building tool arguments / follow-up prompts from state
# ---------------------------------------------------------------------------

def _extract_code(prompt: str) -> str:
    """
    Pull code out of the prompt if it's fenced in a ``` code block; otherwise
    fall back to treating the whole prompt as the code to run. This is a
    deliberately simple heuristic — Person F does not yet generate code via
    Qwen before executing it; that's a future refinement, not required by
    the current contract scope.
    """
    if "```" in prompt:
        parts = prompt.split("```")
        if len(parts) >= 2:
            block = parts[1]
            # strip an optional leading language tag, e.g. ```python
            first_line, _, rest = block.partition("\n")
            if rest and first_line.strip().isalpha():
                return rest.strip()
            return block.strip()
    return prompt.strip()


def _detect_file_type(prompt: str) -> str:
    lowered = prompt.lower()
    if any(kw in lowered for kw in ("xlsx", "excel", "spreadsheet")):
        return "xlsx"
    if any(kw in lowered for kw in ("pptx", "powerpoint", "slide", "presentation")):
        return "pptx"
    return "docx"  # default per problem statement's approval-note use case


def _build_generate_file_args(prompt: str) -> dict:
    file_type = _detect_file_type(prompt)
    title = prompt.strip().splitlines()[0][:80] or "Generated Document"
    content = {
        "title": title,
        "sections": [
            {"heading": "Details", "body": prompt.strip()},
        ],
    }
    return {"file_type": file_type, "content": content}


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


def _build_code_result_prompt(original_prompt: str, tool_observation: dict) -> str:
    stdout = tool_observation.get("stdout", "")
    stderr = tool_observation.get("stderr", "")
    exit_code = tool_observation.get("exit_code")
    return (
        f"The following code was executed to satisfy this request: \"{original_prompt}\"\n\n"
        f"exit_code: {exit_code}\n"
        f"stdout:\n{stdout}\n"
        f"stderr:\n{stderr}\n\n"
        f"Using this VERIFIED execution result (never re-derive the arithmetic yourself), "
        f"write a short answer to the original request for the user."
    )


def _build_docsearch_prompt(original_prompt: str, tool_observation: dict) -> str:
    results = tool_observation.get("results", [])
    if not results:
        passages = "(no matching passages found in the local document store)"
    else:
        passages = "\n\n".join(
            f"[Source: {r.get('source', 'unknown')}] {r.get('text', '')}" for r in results
        )
    return (
        f"Using ONLY the following passages retrieved from the local document store, "
        f"answer this request: \"{original_prompt}\"\n\n"
        f"---\n{passages}\n---\n\n"
        f"If the passages don't contain enough information, say so explicitly rather than guessing."
    )


def _tool_attempt_count(state: TaskState, tool_name: str) -> int:
    return sum(
        1 for r in state.step_records
        if r.action == "call_tool" and r.tool_name == tool_name
    )


# ---------------------------------------------------------------------------
# Core planner
# ---------------------------------------------------------------------------

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

        if state.task_type == "code-execution":
            print(f"[PLANNER] task_id={state.task_id} step0 -> call_tool(execute_code)")
            return NextStep(
                action="call_tool",
                tool_name="execute_code",
                tool_args={"code": _extract_code(state.prompt), "language": "python"},
            )

        if state.task_type == "doc-search":
            print(f"[PLANNER] task_id={state.task_id} step0 -> call_tool(search_docs)")
            return NextStep(
                action="call_tool",
                tool_name="search_docs",
                tool_args={"query": state.prompt, "top_k": 3},
            )

        if state.task_type == "document-generation":
            print(f"[PLANNER] task_id={state.task_id} step0 -> call_tool(generate_file)")
            return NextStep(
                action="call_tool",
                tool_name="generate_file",
                tool_args=_build_generate_file_args(state.prompt),
            )

        # text-generation (default) -> single Qwen call
        print(f"[PLANNER] task_id={state.task_id} step0 -> call_qwen (text entry point)")
        return NextStep(action="call_qwen", model=TEXT_MODEL, prompt=state.prompt)

    # --- Step 1+: replan based on what happened last ---
    last = state.step_records[-1]

    if last.status == "error":
        # Tool calls get a bounded retry; model calls and repeated tool
        # failures finalize with the error surfaced to the caller.
        if last.action == "call_tool" and last.tool_name:
            attempts = _tool_attempt_count(state, last.tool_name)
            if attempts < MAX_TOOL_ATTEMPTS:
                print(
                    f"[PLANNER] task_id={state.task_id} tool '{last.tool_name}' failed "
                    f"(attempt {attempts}/{MAX_TOOL_ATTEMPTS}) -> retrying same tool call"
                )
                return NextStep(
                    action="call_tool",
                    tool_name=last.tool_name,
                    tool_args=_rebuild_tool_args(state, last),
                )
            print(
                f"[PLANNER] task_id={state.task_id} tool '{last.tool_name}' failed "
                f"{attempts}x -> giving up, finalize with error"
            )
            return NextStep(action="finalize")

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
        # Whether this was the plain text entry point or a post-tool /
        # post-Moondream reasoning step, a completed Qwen call is terminal.
        print(f"[PLANNER] task_id={state.task_id} qwen observed -> finalize")
        return NextStep(action="finalize")

    if last.action == "call_tool":
        if last.tool_name == "execute_code":
            reasoning_prompt = _build_code_result_prompt(state.prompt, last.observation or {})
            print(f"[PLANNER] task_id={state.task_id} execute_code observed -> chaining to call_qwen")
            return NextStep(action="call_qwen", model=TEXT_MODEL, prompt=reasoning_prompt)

        if last.tool_name == "search_docs":
            reasoning_prompt = _build_docsearch_prompt(state.prompt, last.observation or {})
            print(f"[PLANNER] task_id={state.task_id} search_docs observed -> chaining to call_qwen")
            return NextStep(action="call_qwen", model=TEXT_MODEL, prompt=reasoning_prompt)

        if last.tool_name == "generate_file":
            # File is the deliverable itself — do not let Qwen describe it,
            # just finalize with the file result already in the observation.
            print(f"[PLANNER] task_id={state.task_id} generate_file observed -> finalize (file is the result)")
            return NextStep(action="finalize")

        print(f"[PLANNER] task_id={state.task_id} unknown tool_name '{last.tool_name}' -> finalize")
        return NextStep(action="finalize")

    # Any other/unhandled action falls through to finalize defensively.
    print(f"[PLANNER] task_id={state.task_id} unhandled last action '{last.action}' -> finalize")
    return NextStep(action="finalize")


def _rebuild_tool_args(state: TaskState, failed_step) -> dict:
    """
    Rebuilds the same tool call's args for a retry. Kept as a thin
    re-derivation from state.prompt (not from failed_step) so a retry
    doesn't blindly resend malformed args if the original computation
    is cheap and deterministic to redo.
    """
    if failed_step.tool_name == "execute_code":
        return {"code": _extract_code(state.prompt), "language": "python"}
    if failed_step.tool_name == "search_docs":
        return {"query": state.prompt, "top_k": 3}
    if failed_step.tool_name == "generate_file":
        return _build_generate_file_args(state.prompt)
    return {}
