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
from router.router import route_task
from executor.state import TaskState
from executor.planner import (
    decide_next_step,
    NextStep,
    FILEGEN_CODE_MARKER,
    FILEGEN_CONTENT_MARKER,
)
from clients.inference_client import call_inference
from clients.tools_client import execute_code, search_docs, generate_file
from schemas.task import ExecuteTaskRequest, ExecuteTaskResponse, TaskResult

_FILEGEN_MARKERS = (FILEGEN_CODE_MARKER, FILEGEN_CONTENT_MARKER)


def _strip_filegen_marker(prompt: str) -> str:
    """Marker prefixes are internal stage tags for the planner — Qwen itself
    should never see them, only the actual prompt text that follows."""
    for marker in _FILEGEN_MARKERS:
        if prompt.startswith(marker):
            return prompt[len(marker):]
    return prompt


async def run_agent_loop(req: ExecuteTaskRequest) -> ExecuteTaskResponse:
    # Built fresh per call (not at module import time) so that patching
    # execute_code/search_docs/generate_file at the module level — e.g. in
    # tests via `patch("executor.loop.execute_code", ...)` — is honored.
    tool_dispatch = {
        "execute_code": execute_code,
        "search_docs": search_docs,
        "generate_file": generate_file,
    }

    state = TaskState(
        task_id=req.task_id,
        prompt=req.prompt,
        file_base64=req.file_base64,
        file_mime_type=req.file_mime_type,
    )

    print(f"[LOOP] task_id={req.task_id} START prompt={req.prompt!r} has_file={bool(req.file_base64)}")

    try:
        task_type, first_model, needs_reasoning = await route_task(req.prompt, req.file_mime_type)
        state.task_type = task_type
        state.needs_reasoning = needs_reasoning

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
                    response_text = await call_inference(
                        model=next_step.model,
                        prompt=sent_prompt,
                        image_base64=next_step.image_base64,
                    )
                    print(
                        f"[LOOP] task_id={state.task_id} model={next_step.model} "
                        f"response_preview={str(response_text)[:120]!r}"
                    )
                    state.add_step(
                        action=next_step.action,
                        model_used=next_step.model,
                        prompt_used=next_step.prompt,
                        observation=response_text,
                        status="ok",
                    )
                except Exception as step_exc:  # noqa: BLE001
                    print(f"[LOOP] task_id={state.task_id} model={next_step.model} ERROR: {step_exc}")
                    state.add_step(
                        action=next_step.action,
                        model_used=next_step.model,
                        prompt_used=next_step.prompt,
                        observation=None,
                        status="error",
                        error=str(step_exc),
                    )
                    state.error = str(step_exc)
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
                        model_used=None,
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
                    print(f"[LOOP] task_id={state.task_id} tool={next_step.tool_name} ERROR: {tool_exc}")
                    state.add_step(
                        action="call_tool",
                        model_used=None,
                        prompt_used=str(tool_args),
                        observation=None,
                        status="error",
                        error=str(tool_exc),
                        tool_name=next_step.tool_name,
                        tool_args=tool_args,
                    )
                    state.error = str(tool_exc)
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
            )

        # File-generation tasks finalize straight off generate_file's
        # observation — the file itself is the deliverable, not model text.
        if (
            last_step
            and last_step.action == "call_tool"
            and last_step.tool_name == "generate_file"
            and last_step.status == "ok"
        ):
            file_obs = last_step.observation or {}
            print(
                f"[LOOP] task_id={state.task_id} END status=completed "
                f"result_type=file file_url={file_obs.get('file_url')}"
            )
            return ExecuteTaskResponse(
                status="completed",
                model_used=state.model_used,
                task_type=state.task_type,
                result=TaskResult(
                    type="file",
                    text=None,
                    file_url=file_obs.get("file_url"),
                    file_name=file_obs.get("file_name"),
                ),
                error=None,
            )

        final_text = state.last_observation or ""
        print(
            f"[LOOP] task_id={state.task_id} END status=completed "
            f"final_model={state.model_used} steps_run={state.step_count}"
        )
        return ExecuteTaskResponse(
            status="completed",
            model_used=state.model_used,
            task_type=state.task_type,
            result=TaskResult(type="text", text=str(final_text)),
            error=None,
        )

    except Exception as exc:  # noqa: BLE001
        print(f"[LOOP] task_id={req.task_id} UNCAUGHT ERROR: {exc}")
        return ExecuteTaskResponse(
            status="failed",
            model_used=state.model_used,
            task_type=state.task_type,
            result=TaskResult(type="text", text=None),
            error=str(exc),
        )
