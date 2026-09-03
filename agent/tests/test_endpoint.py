"""
§2.10 shape check: send a real request, diff response against contract shape.
Run with: pytest tests/test_endpoint.py
"""
import pytest
from httpx import AsyncClient, ASGITransport
from main import app


@pytest.mark.asyncio
async def test_execute_task_shape():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/execute-task",
            json={
                "task_id": "11111111-1111-1111-1111-111111111111",
                "prompt": "hello, just say hi back",
                "file_base64": None,
                "file_mime_type": None,
            },
        )
    assert resp.status_code == 200
    data = resp.json()
    for key in ("status", "model_used", "task_type", "result", "error"):
        assert key in data
    assert data["status"] in ("completed", "failed")
    assert data["result"]["type"] in ("text", "file")
