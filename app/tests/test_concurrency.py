"""
§2.4 critical rule / §2.10 item 5: fire two tasks at the literal same
moment and confirm they genuinely overlap (the service must not serialize
onto one slot).

Pre-single-node-refactor, POST /execute-task was itself synchronous and
blocked for the whole task's duration, so measuring the HTTP call's own
wall-clock start/end was a valid overlap check. The real, browser-facing
contract is now fire-and-forget: POST /api/submit-task returns
{"task_id","status":"queued"} immediately, and the actual work happens in
the background (app/api/dispatch.py's asyncio.create_task).

This test mocks call_inference with an artificial delay (rather than
depending on a real, fast Ollama round-trip) for two reasons: (1) it makes
the check deterministic and fast instead of racing against real network/
inference timing, and (2) the task-status audit timestamps only have
second-level resolution (see the DB schema in app/audit/log.py), which is
too coarse to prove overlap for two genuinely-fast real calls that can
complete inside the same second — a real end-to-end concurrency check
against live Ollama still exists in test_endpoint.py.
"""
import asyncio
import time

import pytest
from unittest.mock import AsyncMock, patch
from httpx import AsyncClient, ASGITransport
from app.main import app

ARTIFICIAL_DELAY_SECONDS = 0.5


async def _slow_call_inference(*args, **kwargs):
    await asyncio.sleep(ARTIFICIAL_DELAY_SECONDS)
    return "a slow response"


@pytest.mark.asyncio
async def test_two_requests_overlap():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:

        async def submit(prompt: str) -> str:
            resp = await client.post(
                "/api/submit-task",
                json={"user_id": "concurrency-test", "prompt": prompt, "file_base64": None,
                      "file_name": None, "file_mime_type": None},
            )
            assert resp.status_code == 200
            return resp.json()["task_id"]

        async def wait_for_completion(task_id: str) -> tuple:
            start = time.monotonic()
            for _ in range(40):
                await asyncio.sleep(0.05)
                resp = await client.get(f"/api/task-status/{task_id}")
                data = resp.json()
                if data["status"] in ("completed", "failed"):
                    return start, time.monotonic(), data
            raise TimeoutError(f"task {task_id} did not finish in time")

        with patch("app.agent.loop.call_inference", new=AsyncMock(side_effect=_slow_call_inference)):
            task_id_1, task_id_2 = await asyncio.gather(
                submit("write two sentences about vessel inspection safety"),
                submit("write two sentences about pump maintenance schedules"),
            )
            (s1, e1, data1), (s2, e2, data2) = await asyncio.gather(
                wait_for_completion(task_id_1),
                wait_for_completion(task_id_2),
            )

    assert data1["status"] == "completed"
    assert data2["status"] == "completed"

    overlap = max(s1, s2) < min(e1, e2)
    assert overlap, (
        f"Task processing windows did not overlap (task 1: {e1 - s1:.2f}s, "
        f"task 2: {e2 - s2:.2f}s) — service may be serializing tasks!"
    )
    # Two 0.5s-delayed tasks running genuinely in parallel finish in ~0.5s
    # total, not ~1s — a real overlap, not just "didn't error while waiting."
    assert max(e1, e2) - min(s1, s2) < ARTIFICIAL_DELAY_SECONDS * 1.8
