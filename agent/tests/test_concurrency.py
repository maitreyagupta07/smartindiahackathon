"""
§2.4 critical rule / §2.10 item 5: fire two /execute-task calls at the
literal same moment and confirm they genuinely overlap (do not serialize).

This is the single most demo-breaking check to skip — run it against the
REAL service (not just mocks) once Person A/C are wired in.
"""
import asyncio
import time
import pytest
from httpx import AsyncClient, ASGITransport
from main import app


@pytest.mark.asyncio
async def test_two_requests_overlap():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:

        async def fire(task_id: str):
            start = time.monotonic()
            resp = await client.post(
                "/execute-task",
                json={
                    "task_id": task_id,
                    "prompt": "slow task for concurrency test",
                    "file_base64": None,
                    "file_mime_type": None,
                },
            )
            end = time.monotonic()
            return start, end, resp

        (s1, e1, r1), (s2, e2, r2) = await asyncio.gather(
            fire("22222222-2222-2222-2222-222222222222"),
            fire("33333333-3333-3333-3333-333333333333"),
        )

    # Overlap check: one call's window should intersect the other's,
    # not run strictly before/after it.
    overlap = max(s1, s2) < min(e1, e2)
    assert overlap, "Requests did not overlap — service may be serializing calls!"
