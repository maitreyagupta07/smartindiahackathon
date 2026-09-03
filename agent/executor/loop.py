"""
Real multi-step execute -> observe -> replan agent loop.

Must remain async all the way down (contract §2.4 critical rule) — no
blocking calls anywhere in this chain. This is what run_agent_loop() does
on every /execute-task call:

    route_task()                         # decide entry point + task_type
    loop:
        decide_next_step(state)          # PLAN  (executor/planner.py)
        dispatch action -> call model    # ACT   (clients/inference_client.py)
        state.add_step(...)              # OBSERVE (persistent state)
        # loop repeats -> planner re-plans against the new observation
    build ExecuteTaskResponse            # exact §2.4 contract shape, unchanged

Supports multi-model orchestration in a single task (Moondream -> Qwen),
persistent step/observation history, and hard max-step protection so a
planner bug can never hang a request indefinitely.

Tool-calling (Person C) is NOT implemented yet — see the call_tool branch
below for the clean extension point. Do not depend on Person C's service
from this file until that contract is confirmed live.
"""
from router.router import route_task
from executor.state import TaskState
from executor.planner import decide_next_step, NextStep
from clients.inference_client import call_inference
from schemas.task import ExecuteTaskRequest, ExecuteTaskResponse, TaskResult


async def run_agent_loop(req: ExecuteTaskRequest) -> ExecuteTaskResponse:
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
                f"planned_action={next_step.action} model={next_step.model}"
            )

            if next_step.action == "finalize":
                state.finished = True
                break

            if next_step.action in ("call_qwen", "call_moondream"):
                try:
                    response_text = await call_inference(
                        model=next_step.model,
                        prompt=next_step.prompt,
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
                    # Let the loop run one more iteration so the planner
                    # observes the error and finalizes cleanly (planner
                    # already handles status == "error" -> finalize).

            elif next_step.action == "call_tool":
                # --- Extension point for Person C's tools ---
                # Not implemented in this phase. The planner never emits
                # this action today (see executor/planner.py docstring),
                # so this branch should be unreachable right now. Kept
                # explicit (rather than omitted) so wiring in
                # clients/tools_client.execute_code / search_docs /
                # generate_file later is a two-file change (planner.py +
                # this branch), not a redesign.
                print(f"[LOOP] task_id={state.task_id} call_tool requested but NOT IMPLEMENTED — finalizing")
                state.add_step(
                    action="call_tool",
                    model_used=None,
                    prompt_used=next_step.prompt,
                    observation=None,
                    status="error",
                    error="Tool calling not implemented yet (Person C not wired in).",
                )
                state.error = "Tool calling not implemented yet."
                state.finished = True

            else:
                # Defensive — planner contract only returns the four known
                # actions, but fail loudly instead of looping forever.
                print(f"[LOOP] task_id={state.task_id} UNKNOWN action '{next_step.action}' -> finalizing")
                state.error = f"Unknown planner action: {next_step.action}"
                state.finished = True

        # --- Build final response, per exact §2.4 contract shape ---
        if state.error:
            print(f"[LOOP] task_id={state.task_id} END status=failed error={state.error!r}")
            return ExecuteTaskResponse(
                status="failed",
                model_used=state.model_used,
                task_type=state.task_type,
                result=TaskResult(type="text", text=None),
                error=state.error,
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
            result=TaskResult(type="text", text=final_text),
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
