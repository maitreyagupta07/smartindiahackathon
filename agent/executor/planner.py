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
     -> [if the request is computational/numerical]
          call_qwen (generate a Python script that computes the needed data)
            -> observe generated code
            -> call_tool(execute_code)  (Person C — verifies/computes the real data)
               -> observe verified stdout
               -> call_qwen (content-prep: turn original prompt + verified data
                              into structured FileContent JSON)
       [else]
          call_qwen (content-prep: turn original prompt directly into
                      structured FileContent JSON)
        -> observe structured content JSON
        -> call_tool(generate_file) using the PREPARED structured content
           (never the raw user prompt)
           -> observe file_url/file_name
           -> finalize

     Person F never hands Person C's FileContent schema a copy of the raw
     user prompt. A content-preparation stage (via Qwen, optionally grounded
     in verified execute_code output) always produces the {"title",
     "sections":[{"heading","body"}]} structure first.

Tool dispatch uses Person C's existing endpoints exactly as contracted in
§2.6, via clients/tools_client.py — no duplicate tool logic lives here,
only the decision of WHEN to call which tool and what to pass it.
"""
import json
import re
from dataclasses import dataclass
from typing import Literal, Optional

from executor.state import TaskState
from router.model_registry import TEXT_MODEL, VISION_MODEL, LORA_ADAPTER, is_approval_note_request

Action = Literal["call_qwen", "call_moondream", "call_tool", "finalize"]
ToolName = Literal["execute_code", "search_docs", "generate_file"]

# A tool call is allowed at most this many total attempts (initial + retries)
# before the planner gives up and finalizes with an error. Independent of,
# and tighter than, state.max_steps — this bounds retries specifically.
MAX_TOOL_ATTEMPTS = 2

# Internal stage markers prefixed onto call_qwen prompts during the
# document-generation flow so decide_next_step can tell, purely from
# state.step_records (no extra mutable flow-control state), which stage of
# that flow a completed Qwen call belongs to. Never shown to the user —
# stripped before the prompt is actually sent to Qwen.
FILEGEN_CODE_MARKER = "__FILEGEN_CODE__"
FILEGEN_CONTENT_MARKER = "__FILEGEN_CONTENT__"

# Heuristic keywords indicating the file-generation request needs real
# computed/verified data rather than free-form prose — in which case F
# should run execute_code first and ground the file content in its output.
_COMPUTE_KEYWORDS = (
    "calculate", "compute", "computation", "sum", "average", "mean", "median",
    "total", "fibonacci", "prime", "factorial", "sequence", "statistics",
    "count", "sort", "series", "numeric", "numbers",
)


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


def _filegen_model(prompt: str) -> str:
    """
    Approval-note-flavored requests use Person A's fine-tuned LORA_ADAPTER
    (trained specifically on approval-note phrasing/structure) for every
    Qwen call in the document-generation flow; everything else keeps using
    the base TEXT_MODEL. Falls back to TEXT_MODEL if the adapter isn't
    configured yet. Note: whether this request is a document-generation task
    at all (i.e. whether a file actually gets produced) is decided
    separately by the router's classifier — this only picks which model to
    use once we're already in that flow.
    """
    if is_approval_note_request(prompt) and LORA_ADAPTER:
        return LORA_ADAPTER
    return TEXT_MODEL


def _text_model(prompt: str) -> str:
    """
    Same adapter choice as _filegen_model, but for the plain text-generation
    entry point — an approval-note-style request that never mentioned a file
    format (e.g. "make an approval note for the refinery") should still get
    the LoRA adapter's writing style, it just won't produce a file.
    """
    if is_approval_note_request(prompt) and LORA_ADAPTER:
        return LORA_ADAPTER
    return TEXT_MODEL


def _needs_computation(prompt: str) -> bool:
    """
    Heuristic: does this file-generation request depend on real computed/
    numeric data (e.g. "first 20 Fibonacci numbers ... average") rather than
    free-form prose? If so, F should verify the numbers via execute_code
    before preparing file content, instead of trusting Qwen's arithmetic.
    """
    lowered = prompt.lower()
    return any(kw in lowered for kw in _COMPUTE_KEYWORDS)


def _build_filegen_code_prompt(original_prompt: str) -> str:
    """
    Asks Qwen to produce a runnable Python script (executed via Person C's
    execute_code tool) that computes whatever data the file-generation
    request needs, printing it as JSON so the next stage can ground the
    file content in verified output rather than model arithmetic.
    """
    return (
        "You are preparing verified data for a document/spreadsheet generation request.\n"
        f"User request: \"{original_prompt}\"\n\n"
        "Write ONLY a single self-contained Python script (no markdown fences, no "
        "explanation, nothing but code) that computes whatever data the request "
        "needs and prints the final result as JSON to stdout via "
        "`print(json.dumps(result))`. Include every individual item/entry the "
        "request asks for (e.g. all N values, not just a sample) plus any "
        "requested aggregates (sum, average, etc)."
    )


def _build_filegen_content_prompt(original_prompt: str, verified_data: Optional[str]) -> str:
    """
    Asks Qwen to turn the user's request (optionally grounded in verified
    execute_code output) into Person C's FileContent JSON schema. This is
    the step that must NEVER be skipped in favor of copying the raw prompt.
    """
    data_block = (
        f"Verified computed data (use these exact values — do not recompute, "
        f"alter, or shorten them):\n{verified_data}\n\n"
        if verified_data else ""
    )
    return (
        "You are preparing structured content for a generated file.\n"
        f"User request: \"{original_prompt}\"\n\n"
        f"{data_block}"
        "Respond with ONLY valid JSON (no markdown fences, no commentary) matching "
        "exactly this schema:\n"
        '{"title": "<short title>", "sections": [{"heading": "<heading>", "body": "<body text>"}]}\n\n'
        "The content must fully represent what the user actually requested — include "
        "EVERY requested item/entry (not a summary and not the request text itself). "
        "Use multiple sections where that helps (e.g. one section for the data, another "
        "for a requested summary/average). A section's \"body\" may use newlines to lay "
        "out lists/tables as plain text."
    )


def _parse_structured_content(text: str) -> Optional[dict]:
    """
    Best-effort extraction of a FileContent-shaped JSON object out of a Qwen
    response, tolerating stray markdown fences or leading/trailing prose.
    """
    if not text:
        return None
    candidate = text.strip()
    if "```" in candidate:
        for part in candidate.split("```"):
            part = part.strip()
            if part.startswith("{"):
                candidate = part
                break
    try:
        return json.loads(candidate)
    except (json.JSONDecodeError, TypeError):
        pass
    start, end = candidate.find("{"), candidate.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(candidate[start:end + 1])
        except json.JSONDecodeError:
            return None
    return None


def _is_valid_file_content(obj) -> bool:
    return (
        isinstance(obj, dict)
        and isinstance(obj.get("title"), str)
        and isinstance(obj.get("sections"), list)
        and len(obj["sections"]) > 0
        and all(
            isinstance(s, dict) and isinstance(s.get("heading"), str) and isinstance(s.get("body"), str)
            for s in obj["sections"]
        )
    )


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

    if exit_code != 0:
        # The sandbox run failed (e.g. a syntax error because the prompt had
        # no actual code for Person F to extract) — Qwen must report the
        # failure honestly, never invent a plausible-looking "verified"
        # answer on top of a run that didn't actually succeed.
        return (
            f"Code was executed to satisfy this request: \"{original_prompt}\"\n\n"
            f"exit_code: {exit_code}\n"
            f"stdout:\n{stdout}\n"
            f"stderr:\n{stderr}\n\n"
            f"The execution FAILED (non-zero exit code). Do NOT invent or guess a result. "
            f"Tell the user the code execution failed and include the relevant error from stderr."
        )

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
            state.file_type = _detect_file_type(state.prompt)
            filegen_model = _filegen_model(state.prompt)
            if _needs_computation(state.prompt):
                print(
                    f"[PLANNER] task_id={state.task_id} step0 -> call_qwen model={filegen_model} "
                    f"(filegen: generate verification code, computation detected in request)"
                )
                return NextStep(
                    action="call_qwen",
                    model=filegen_model,
                    prompt=FILEGEN_CODE_MARKER + _build_filegen_code_prompt(state.prompt),
                )
            print(
                f"[PLANNER] task_id={state.task_id} step0 -> call_qwen model={filegen_model} "
                f"(filegen: content-preparation stage"
                f"{', using approval-note LoRA adapter' if filegen_model == LORA_ADAPTER else ''})"
            )
            return NextStep(
                action="call_qwen",
                model=filegen_model,
                prompt=FILEGEN_CONTENT_MARKER + _build_filegen_content_prompt(state.prompt, None),
            )

        # text-generation (default) -> single Qwen call
        text_model = _text_model(state.prompt)
        print(
            f"[PLANNER] task_id={state.task_id} step0 -> call_qwen model={text_model} "
            f"(text entry point{', using approval-note LoRA adapter' if text_model == LORA_ADAPTER else ''})"
        )
        return NextStep(action="call_qwen", model=text_model, prompt=state.prompt)

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
        if state.task_type == "document-generation" and (last.prompt_used or "").startswith(FILEGEN_CODE_MARKER):
            code = _extract_code(str(last.observation))
            print(
                f"[PLANNER] task_id={state.task_id} filegen verification code generated "
                f"-> call_tool(execute_code) to compute real data before content-prep"
            )
            return NextStep(
                action="call_tool",
                tool_name="execute_code",
                tool_args={"code": code, "language": "python"},
            )

        if state.task_type == "document-generation" and (last.prompt_used or "").startswith(FILEGEN_CONTENT_MARKER):
            content = _parse_structured_content(str(last.observation))
            if not _is_valid_file_content(content):
                print(
                    f"[PLANNER] task_id={state.task_id} filegen content-prep returned invalid JSON "
                    f"-> falling back to raw-prompt content"
                )
                content = _build_generate_file_args(state.prompt)["content"]
            state.prepared_file_content = content
            file_type = state.file_type or _detect_file_type(state.prompt)
            print(
                f"[PLANNER] task_id={state.task_id} filegen content prepared "
                f"(title={content.get('title')!r}, sections={len(content.get('sections', []))}) "
                f"-> call_tool(generate_file) file_type={file_type}"
            )
            return NextStep(
                action="call_tool",
                tool_name="generate_file",
                tool_args={"file_type": file_type, "content": content},
            )

        # Whether this was the plain text entry point or a post-tool /
        # post-Moondream reasoning step, a completed Qwen call is terminal.
        print(f"[PLANNER] task_id={state.task_id} qwen observed -> finalize")
        return NextStep(action="finalize")

    if last.action == "call_tool":
        if last.tool_name == "execute_code":
            if state.task_type == "document-generation":
                stdout = (last.observation or {}).get("stdout", "")
                filegen_model = _filegen_model(state.prompt)
                print(
                    f"[PLANNER] task_id={state.task_id} filegen verification code executed "
                    f"-> chaining to call_qwen model={filegen_model} "
                    f"(content-prep stage, grounded in verified stdout"
                    f"{', using approval-note LoRA adapter' if filegen_model == LORA_ADAPTER else ''})"
                )
                return NextStep(
                    action="call_qwen",
                    model=filegen_model,
                    prompt=FILEGEN_CONTENT_MARKER + _build_filegen_content_prompt(state.prompt, stdout),
                )
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
            file_obs = last.observation or {}
            state.file_url = file_obs.get("file_url")
            state.file_name = file_obs.get("file_name")
            print(
                f"[PLANNER] task_id={state.task_id} generate_file observed "
                f"file_url={state.file_url} file_name={state.file_name} -> finalize (file is the result)"
            )
            return NextStep(action="finalize")

        print(f"[PLANNER] task_id={state.task_id} unknown tool_name '{last.tool_name}' -> finalize")
        return NextStep(action="finalize")

    # Any other/unhandled action falls through to finalize defensively.
    print(f"[PLANNER] task_id={state.task_id} unhandled last action '{last.action}' -> finalize")
    return NextStep(action="finalize")


def _rebuild_tool_args(state: TaskState, failed_step) -> dict:
    """
    Rebuilds the same tool call's args for a retry. Prefers replaying the
    exact args the failed attempt used (e.g. Qwen-generated code, or
    already-prepared file content) — recomputing from state.prompt is only
    a defensive fallback for a step that somehow recorded no tool_args.
    """
    if failed_step.tool_args:
        return failed_step.tool_args
    if failed_step.tool_name == "execute_code":
        return {"code": _extract_code(state.prompt), "language": "python"}
    if failed_step.tool_name == "search_docs":
        return {"query": state.prompt, "top_k": 3}
    if failed_step.tool_name == "generate_file":
        return _build_generate_file_args(state.prompt)
    return {}
