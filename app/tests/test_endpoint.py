"""
§2.10 shape check: send a real request through the single browser-facing
contract and diff the response against the exact shape.

Pre-single-node-refactor, this hit Person F's internal /execute-task
endpoint directly. That endpoint no longer exists as its own HTTP route —
the agent loop is called in-process now (app/api/dispatch.py) — so this
test now goes through the ACTUAL thing a browser calls: POST
/api/submit-task, then poll GET /api/task-status/{id} until it finishes.
This is a real integration test: it needs Ollama actually running (and,
for a file/tool-using prompt, Docker/ChromaDB too) — same as the old test
did against the real agent service.

Run with: pytest app/tests/test_endpoint.py
"""
import asyncio

import pytest
from httpx import AsyncClient, ASGITransport
from app.main import app


@pytest.mark.asyncio
async def test_submit_and_status_shape():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        submit_resp = await client.post(
            "/api/submit-task",
            json={
                "user_id": "test-user",
                "prompt": "hello, just say hi back",
                "file_base64": None,
                "file_name": None,
                "file_mime_type": None,
            },
        )
        assert submit_resp.status_code == 200
        submit_data = submit_resp.json()
        assert "task_id" in submit_data
        assert submit_data["status"] == "queued"
        task_id = submit_data["task_id"]

        status_data = None
        for _ in range(60):  # up to ~30s for a real Ollama call to finish
            await asyncio.sleep(0.5)
            status_resp = await client.get(f"/api/task-status/{task_id}")
            assert status_resp.status_code == 200
            status_data = status_resp.json()
            if status_data["status"] in ("completed", "failed"):
                break

    assert status_data is not None
    for key in ("task_id", "status", "model_used", "started_at", "completed_at", "result", "error"):
        assert key in status_data
    assert status_data["status"] in ("completed", "failed")
    assert status_data["result"]["type"] in ("text", "file", None)


def test_admin_endpoints_require_admin_auth():
    """The audit log and network status are the Admin panel's data. The
    panel itself was passcode-gated in the browser, but these endpoints
    answered any unauthenticated caller on the LAN — the full activity
    record (user ids, client IPs, timestamps) was effectively public."""
    from fastapi.testclient import TestClient
    from app.main import app

    c = TestClient(app)
    for path in ("/api/audit-log", "/api/network-status"):
        assert c.get(path).status_code == 401, path

    # A signed-in NON-admin still cannot read them.
    import uuid
    uid = f"nonadmin-{uuid.uuid4().hex[:10]}"
    # First account on a fresh DB is admin, so make sure one exists first.
    c.post("/api/auth/signup", json={"user_id": f"seed-{uuid.uuid4().hex[:8]}", "password": "pw-test-1234"})
    r = c.post("/api/auth/signup", json={"user_id": uid, "password": "pw-test-1234"})
    assert r.status_code == 200, r.text
    hdrs = {"Authorization": f"Bearer {r.json()['token']}"}
    for path in ("/api/audit-log", "/api/network-status"):
        assert c.get(path, headers=hdrs).status_code == 403, path
