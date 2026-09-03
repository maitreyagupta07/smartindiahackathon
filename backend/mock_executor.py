"""
Throwaway mock of Person F's /execute-task (§2.4), for testing Person B alone.
NOT part of the real deliverable — delete/ignore once Person F's real service is up.
Run on port 8002 same as the real thing so main.py needs zero changes to swap.
"""
import asyncio
from fastapi import FastAPI
from pydantic import BaseModel
from typing import Optional

app = FastAPI()


class ExecuteTaskRequest(BaseModel):
    task_id: str
    prompt: str
    file_base64: Optional[str] = None
    file_mime_type: Optional[str] = None


@app.post("/execute-task")
async def execute_task(req: ExecuteTaskRequest):
    await asyncio.sleep(3)  # simulate work, lets you test overlapping timestamps
    task_type = "vision" if req.file_mime_type else "text-generation"
    model_used = "moondream" if req.file_mime_type else "qwen2.5:1.5b-instruct"
    return {
        "status": "completed",
        "model_used": model_used,
        "task_type": task_type,
        "result": {
            "type": "text",
            "text": f"[mock response to]: {req.prompt}",
            "file_url": None,
            "file_name": None,
        },
        "error": None,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8002)
