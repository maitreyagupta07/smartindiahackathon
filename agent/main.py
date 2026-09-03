"""
Person F — Agent Loop / Task Executor
Entry point: FastAPI app exposing POST /execute-task per contract §2.4
"""
from fastapi import FastAPI
from schemas.task import ExecuteTaskRequest, ExecuteTaskResponse
from executor.loop import run_agent_loop

app = FastAPI(title="Agent Loop / Task Executor - Person F")


@app.post("/execute-task", response_model=ExecuteTaskResponse)
async def execute_task(req: ExecuteTaskRequest) -> ExecuteTaskResponse:
    """
    Contract §2.4: must be truly async / non-blocking so multiple
    simultaneous calls do not serialize onto one thread.
    """
    result = await run_agent_loop(req)
    return result


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8002, reload=True)
