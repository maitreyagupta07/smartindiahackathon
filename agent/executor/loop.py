"""
Core plan -> act -> observe -> finalize loop.
Must remain async all the way down (contract §2.4 critical rule) —
no blocking calls anywhere in this chain.
"""
from router.router import route_task
from executor.state import TaskState
from executor.planner import plan_next_action
from clients.inference_client import call_inference
from clients.tools_client import execute_code, search_docs, generate_file
from schemas.task import ExecuteTaskRequest, ExecuteTaskResponse, TaskResult


async def run_agent_loop(req: ExecuteTaskRequest) -> ExecuteTaskResponse:
    state = TaskState(
        task_id=req.task_id,
        prompt=req.prompt,
        file_base64=req.file_base64,
        file_mime_type=req.file_mime_type,
    )

    try:
        task_type, model_name = await route_task(req.prompt, req.file_mime_type)
        state.task_type = task_type
        state.model_used = model_name

        while not state.finished:
            action = plan_next_action(state)

            if action == "call_model":
                response_text = await call_inference(
                    model=model_name,
                    prompt=req.prompt,
                    image_base64=req.file_base64 if task_type == "vision" else None,
                )
                state.add_step("call_model", response_text)

            elif action == "call_tool":
                # placeholder branch — wire in execute_code / search_docs /
                # generate_file here as the real planner grows
                state.add_step("call_tool", None)

            elif action == "finalize":
                state.finished = True

        final_text = state.steps[-1]["observation"] if state.steps else ""

        return ExecuteTaskResponse(
            status="completed",
            model_used=state.model_used,
            task_type=state.task_type,
            result=TaskResult(type="text", text=final_text),
            error=None,
        )

    except Exception as exc:  # noqa: BLE001
        return ExecuteTaskResponse(
            status="failed",
            model_used=state.model_used,
            task_type=state.task_type,
            result=TaskResult(type="text", text=None),
            error=str(exc),
        )
