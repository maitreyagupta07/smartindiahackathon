"""
Real multi-step execute -> observe -> replan agent loop.

Must remain async all the way down (contract §2.4 critical rule) — no
blocking calls anywhere in this chain. This is what run_agent_loop() does
on every /execute-task call:

    route_task()                         # decide entry point + task_type
    loop:
        decide_next_step(state)          # PLAN    (executor/planner.py)
        dispatch action -> model or tool # ACT     (clients/*.py)
        state.add_step(...)              # OBSERVE (persistent state)
        # loop repeats -> planner re-plans against the new observation
    build ExecuteTaskResponse            # exact §2.4 contract shape, unchanged

Supports multi-model orchestration (Moondream -> Qwen) AND tool
orchestration (execute_code / search_docs / generate_file, all via Person
C's existing endpoints through clients/tools_client.py — no duplicate tool
logic lives here), persistent step/observation history, and hard max-step
protection so a planner bug can never hang a request indefinitely.
"""
from ..router.router import route_task
from ..router.classifier import classify as classify_prompt
from .state import TaskState
from .planner import (
    decide_next_step,
    NextStep,
    FILEGEN_CODE_MARKER,
    FILEGEN_CONTENT_MARKER,
    strip_markdown_emphasis,
    SUPPORTED_WORKFLOW_STAGES,
)
from ..inference.client import call_inference
from ..tools.facade import execute_code, search_docs, generate_file, scan_document, generate_image, forecast_timeseries
from ..tools.pdf_extract import (
    is_pdf as is_pdf_mime,
    has_no_text_layer,
    render_first_page_png_base64,
    extract_text_from_pdf,
)
from ..router.model_registry import IMAGE_MODEL, TIMESERIES_MODEL
from ..schemas.task import ExecuteTaskRequest, ExecuteTaskResponse, TaskResult

# generate_image/forecast_timeseries are genuinely backed by a real local
# model (SD Turbo / MOMENT-1-small) unlike the other four tools (plain code
# execution, doc search, file writing, OCR) — worth surfacing as
# `model_used`/models_used, same as call_qwen/call_moondream steps do,
# instead of the None every other tool call correctly passes.
_TOOL_MODEL_LABELS = {"generate_image": IMAGE_MODEL, "forecast_timeseries": TIMESERIES_MODEL}

_FILEGEN_MARKERS = (FILEGEN_CODE_MARKER, FILEGEN_CONTENT_MARKER)


def _strip_filegen_marker(prompt: str) -> str:
    """Marker prefixes are internal stage tags for the planner — Qwen itself
    should never see them, only the actual prompt text that follows."""
    for marker in _FILEGEN_MARKERS:
        if prompt.startswith(marker):
            return prompt[len(marker):]
    return prompt


def _exc_message(exc: Exception) -> str:
    """
    str(exc) is EMPTY for some real exceptions — notably httpx's own
    ReadTimeout, observed live: a slow Ollama call hit call_inference's
    120s timeout, str(the resulting httpx.ReadTimeout) was "", and
    state.error got set to that empty string. `if state.error:` (a
    truthiness check, not an existence check) then treated the empty
    string as "no error", and the task silently finished as status=
    "completed" with a blank text result instead of status="failed"
    with a clear message. Falling back to the exception's type name
    guarantees state.error is always a genuinely truthy, non-empty
    string whenever a real exception occurred.
    """
    return str(exc) or f"{type(exc).__name__} (no further detail from the exception itself)"


async def run_agent_loop(req: ExecuteTaskRequest) -> ExecuteTaskResponse:
    # Built fresh per call (not at module import time) so that patching
    # execute_code/search_docs/generate_file at the module level — e.g. in
    # tests via `patch("executor.loop.execute_code", ...)` — is honored.
    tool_dispatch = {
        "execute_code": execute_code,
        "search_docs": search_docs,
        "generate_file": generate_file,
        "scan_document": scan_document,
        "generate_image": generate_image,
        "forecast_timeseries": forecast_timeseries,
    }

    state = TaskState(
        task_id=req.task_id,
        prompt=req.prompt,
        file_base64=req.file_base64,
        file_mime_type=req.file_mime_type,
        chat_id=getattr(req, "chat_id", None),
        history=getattr(req, "history", None),
        user_id=getattr(req, "user_id", None),
    )

    print(
        f"[LOOP] task_id={req.task_id} START prompt={req.prompt!r} "
        f"has_file={bool(req.file_base64)} chat_id={getattr(req, 'chat_id', None)!r}"
    )

    # Scanned/handwritten PDF support: an attached PDF with no real text
    # layer (has_no_text_layer) is a genuinely scanned/photographed
    # document, not an exported/typed one — direct extraction alone finds
    # nothing, and Tesseract OCR (extract_text_from_pdf's fallback) is
    # unreliable on handwriting. Rather than adding a second, parallel
    # vision pathway, this reframes the request as the SAME kind of request
    # an uploaded photograph already is: render the page to an image and
    # let the existing vision (Moondream) + reasoning (Qwen) chain in
    # planner.py handle it exactly as it already does for any other image,
    # with whatever OCR text this module still found passed along as extra
    # grounding (see state.pdf_ocr_text, consumed in planner._build_reasoning_prompt).
    #
    # Scoped to ONLY the plain-question fallback case (classify() on the
    # prompt text alone would land on "text-generation", i.e. no stronger
    # signal like doc-search/code-execution/document-generation) so a
    # document-generation request ("turn this scanned form into a Word
    # doc") is untouched here and keeps producing an actual file — it still
    # gets whatever text extract_text_from_pdf found, via
    # planner._ground_prompt_in_pdf_text, just without this vision detour.
    if req.file_base64 and is_pdf_mime(req.file_mime_type) and has_no_text_layer(req.file_base64):
        prompt_only_type = classify_prompt(req.prompt, None).task_type
        if prompt_only_type == "text-generation":
            page_image = render_first_page_png_base64(req.file_base64)
            if page_image:
                print(
                    f"[LOOP] task_id={req.task_id} scanned/handwritten PDF detected "
                    f"(no selectable text layer) -> routing the rendered first page "
                    f"through the vision model instead of the bare text prompt"
                )
                state.pdf_ocr_text = extract_text_from_pdf(req.file_base64) or None
                state.file_base64 = page_image
                state.file_mime_type = "image/png"

    try:
        task_type, first_model, needs_reasoning = await route_task(
            req.prompt, state.file_mime_type, getattr(req, "chat_id", None)
        )
        state.task_type = task_type
        state.needs_reasoning = needs_reasoning

        # Multi-tool workflow: classify() (app/router/classifier.py) already
        # detects when a prompt genuinely names more than one distinct
        # tool/task signal (is_multi_step/workflow, scored the exact same
        # way task_type itself is) — previously computed but only ever used
        # for a debug log line in router.py. Reusing that SAME signal here,
        # rather than a second/different relevance system, is what lets one
        # request chain multiple tools instead of being forced through
        # task_type's single flow alone. Only stages this planner has a
        # dedicated gathering handler for (SUPPORTED_WORKFLOW_STAGES) are
        # queued — anything else (e.g. vision, time-series-forecasting,
        # which aren't scored alongside these the same way) is left out
        # rather than guessed at; state.task_type's own single-tool flow is
        # completely unaffected when this queue ends up empty, which is the
        # normal case for an ordinary single-purpose request.
        classification = classify_prompt(req.prompt, state.file_mime_type)
        if classification.is_multi_step and classification.workflow:
            state.pending_stages = [
                t for t in classification.workflow
                if t != task_type and t in SUPPORTED_WORKFLOW_STAGES
            ]
            if state.pending_stages:
                # Each queued stage costs a small, bounded number of extra
                # steps (its own tool call, plus at most one Qwen call) on
                # top of whatever the primary task_type's own flow already
                # budgets for — the same kind of +N-slack bump
                # planner._filegen_entry_step already does for a
                # multi-deliverable document-generation request.
                state.max_steps += 3 * len(state.pending_stages)
                print(
                    f"[LOOP] task_id={req.task_id} multi-tool workflow detected -> "
                    f"queued stages {state.pending_stages} before task_type={task_type!r}'s own flow "
                    f"(max_steps now {state.max_steps})"
                )

        while not state.finished:
            next_step: NextStep = decide_next_step(state)
            print(
                f"[LOOP] task_id={state.task_id} step={state.step_count + 1} "
                f"planned_action={next_step.action} model={next_step.model} tool={next_step.tool_name}"
            )

            if next_step.action == "finalize":
                state.finished = True
                break

            if next_step.action in ("call_qwen", "call_moondream"):
                sent_prompt = _strip_filegen_marker(next_step.prompt or "")
                if sent_prompt != next_step.prompt:
                    print(
                        f"[LOOP] task_id={state.task_id} filegen stage="
                        f"{'code-gen' if next_step.prompt.startswith(FILEGEN_CODE_MARKER) else 'content-prep'} "
                        f"-> call_qwen"
                    )
                try:
                    usage: dict = {}
                    response_text = await call_inference(
                        model=next_step.model,
                        prompt=sent_prompt,
                        image_base64=next_step.image_base64,
                        temperature=next_step.temperature,
                        usage=usage,
                    )
                    print(
                        f"[LOOP] task_id={state.task_id} model={next_step.model} "
                        f"response_preview={str(response_text)[:120]!r} "
                        f"tokens=prompt:{usage.get('prompt_tokens')}/completion:{usage.get('completion_tokens')}"
                    )
                    state.add_step(
                        action=next_step.action,
                        model_used=next_step.model,
                        prompt_used=next_step.prompt,
                        observation=response_text,
                        status="ok",
                        prompt_tokens=usage.get("prompt_tokens"),
                        completion_tokens=usage.get("completion_tokens"),
                    )
                except Exception as step_exc:  # noqa: BLE001
                    message = _exc_message(step_exc)
                    print(f"[LOOP] task_id={state.task_id} model={next_step.model} ERROR: {message}")
                    state.add_step(
                        action=next_step.action,
                        model_used=next_step.model,
                        prompt_used=next_step.prompt,
                        observation=None,
                        status="error",
                        error=message,
                    )
                    state.error = message
                    # Loop continues one more iteration so the planner
                    # observes the error and decides retry/finalize.

            elif next_step.action == "call_tool":
                tool_fn = tool_dispatch.get(next_step.tool_name)
                tool_args = next_step.tool_args or {}

                if tool_fn is None:
                    print(f"[LOOP] task_id={state.task_id} UNKNOWN tool '{next_step.tool_name}'")
                    state.add_step(
                        action="call_tool",
                        model_used=None,
                        prompt_used=None,
                        observation=None,
                        status="error",
                        error=f"Unknown tool requested: {next_step.tool_name!r}",
                        tool_name=next_step.tool_name,
                    )
                    state.error = f"Unknown tool requested: {next_step.tool_name!r}"
                    continue

                try:
                    if next_step.tool_name == "generate_file":
                        print(
                            f"[LOOP] task_id={state.task_id} calling tool=generate_file "
                            f"file_type={tool_args.get('file_type')} "
                            f"content_title={tool_args.get('content', {}).get('title')!r} "
                            f"(prepared content, not raw prompt)"
                        )
                    else:
                        print(f"[LOOP] task_id={state.task_id} calling tool={next_step.tool_name} args={tool_args}")
                    tool_result = await tool_fn(**tool_args)
                    print(f"[LOOP] task_id={state.task_id} tool={next_step.tool_name} OBSERVATION={tool_result}")
                    state.add_step(
                        action="call_tool",
                        model_used=_TOOL_MODEL_LABELS.get(next_step.tool_name),
                        prompt_used=str(tool_args),
                        observation=tool_result,
                        status="ok",
                        tool_name=next_step.tool_name,
                        tool_args=tool_args,
                    )
                    # A successful tool step clears any earlier transient
                    # error state (e.g. this was a retry that succeeded).
                    state.error = None
                except Exception as tool_exc:  # noqa: BLE001
                    message = _exc_message(tool_exc)
                    print(f"[LOOP] task_id={state.task_id} tool={next_step.tool_name} ERROR: {message}")
                    state.add_step(
                        action="call_tool",
                        model_used=None,
                        prompt_used=str(tool_args),
                        observation=None,
                        status="error",
                        error=message,
                        tool_name=next_step.tool_name,
                        tool_args=tool_args,
                    )
                    state.error = message
                    # Loop continues — planner decides retry vs finalize
                    # for tool failures (see planner.py MAX_TOOL_ATTEMPTS).

            else:
                # Defensive — planner contract only returns the four known
                # actions, but fail loudly instead of looping forever.
                print(f"[LOOP] task_id={state.task_id} UNKNOWN action '{next_step.action}' -> finalizing")
                state.error = f"Unknown planner action: {next_step.action}"
                state.finished = True

        # --- Build final response, per exact §2.4 contract shape ---
        last_step = state.step_records[-1] if state.step_records else None

        if state.error and not (last_step and last_step.status == "ok"):
            print(f"[LOOP] task_id={state.task_id} END status=failed error={state.error!r}")
            return ExecuteTaskResponse(
                status="failed",
                model_used=state.model_used,
                task_type=state.task_type,
                result=TaskResult(type="text", text=None),
                error=state.error,
                models_used=state.models_used or None,
                steps=state.step_summary or None,
                token_usage=state.token_totals,
            )

        # File-generation tasks finalize straight off generate_file's/
        # generate_image's observation — the file itself is the
        # deliverable, not model text. Both tools populate
        # state.file_url/file_name/generated_files identically (see
        # planner.py), so one check covers both.
        if (
            last_step
            and last_step.action == "call_tool"
            and last_step.tool_name in ("generate_file", "generate_image")
            and last_step.status == "ok"
        ):
            # state.file_url/file_name (back-compat, always the FIRST file)
            # and state.generated_files (every file, in order — see
            # app/agent/planner.py's _filegen_entry_step for how a
            # multi-deliverable request like "...word doc on X then excel
            # of Y..." now produces more than one) rather than reading
            # last_step.observation directly, which is only ever the LAST
            # generate_file call's own single result.
            print(
                f"[LOOP] task_id={state.task_id} END status=completed "
                f"result_type=file files={state.generated_files}"
            )
            return ExecuteTaskResponse(
                status="completed",
                model_used=state.model_used,
                task_type=state.task_type,
                result=TaskResult(
                    type="file",
                    text=None,
                    file_url=state.file_url,
                    file_name=state.file_name,
                    files=state.generated_files or None,
                ),
                error=None,
                models_used=state.models_used or None,
                steps=state.step_summary or None,
                token_usage=state.token_totals,
            )

        # The LoRA adapter sometimes writes markdown-style **bold** into its
        # output, but nothing renders markdown here — strip it so the plain
        # text answer doesn't show literal asterisks (see
        # planner.strip_markdown_emphasis; the docx path strips it too).
        final_text = strip_markdown_emphasis(state.last_observation or "")
        print(
            f"[LOOP] task_id={state.task_id} END status=completed "
            f"final_model={state.model_used} steps_run={state.step_count}"
        )
        return ExecuteTaskResponse(
            status="completed",
            model_used=state.model_used,
            task_type=state.task_type,
            result=TaskResult(type="text", text=str(final_text), sources=state.sources or None),
            error=None,
            models_used=state.models_used or None,
            steps=state.step_summary or None,
            token_usage=state.token_totals,
        )

    except Exception as exc:  # noqa: BLE001
        print(f"[LOOP] task_id={req.task_id} UNCAUGHT ERROR: {exc}")
        return ExecuteTaskResponse(
            status="failed",
            model_used=state.model_used,
            task_type=state.task_type,
            result=TaskResult(type="text", text=None),
            error=str(exc),
            models_used=state.models_used or None,
            steps=state.step_summary or None,
            token_usage=state.token_totals,
        )
