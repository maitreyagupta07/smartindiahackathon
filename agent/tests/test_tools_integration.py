"""
Tests for Person C tool integration in the agent loop
(executor/planner.py tool selection + executor/loop.py dispatch).

Mocks clients.tools_client functions (and call_inference for the
qwen chaining steps) so no real Person C / Person A services are required —
these validate the ORCHESTRATION logic: which tool gets called, in what
order, with what args, and how tool failures are handled.
"""
import pytest
from unittest.mock import patch, AsyncMock

from schemas.task import ExecuteTaskRequest
from executor.loop import run_agent_loop
from router.model_registry import TEXT_MODEL


@pytest.mark.asyncio
async def test_code_execution_flow_calls_tool_then_qwen():
    req = ExecuteTaskRequest(
        task_id="c1",
        prompt="calculate the sum: ```python\nprint(2+2)\n```",
        file_base64=None,
        file_mime_type=None,
    )
    tool_result = {"stdout": "4\n", "stderr": "", "exit_code": 0}

    with patch("executor.loop.execute_code", new=AsyncMock(return_value=tool_result)) as mocked_tool, \
         patch("executor.loop.call_inference", new=AsyncMock(return_value="The result is 4.")) as mocked_infer:
        resp = await run_agent_loop(req)

    assert resp.status == "completed"
    assert resp.result.type == "text"
    assert resp.result.text == "The result is 4."
    assert resp.model_used == TEXT_MODEL
    mocked_tool.assert_awaited_once()
    assert mocked_tool.call_args.kwargs["language"] == "python"
    mocked_infer.assert_awaited_once()


@pytest.mark.asyncio
async def test_doc_search_flow_calls_tool_then_qwen():
    req = ExecuteTaskRequest(
        task_id="c2",
        prompt="search the manual for hot work permit rules",
        file_base64=None,
        file_mime_type=None,
    )
    tool_result = {"results": [{"text": "Fire watch required.", "source": "sop.txt", "score": 0.9}]}

    with patch("executor.loop.search_docs", new=AsyncMock(return_value=tool_result)) as mocked_tool, \
         patch("executor.loop.call_inference", new=AsyncMock(return_value="Fire watch is required for hot work.")) as mocked_infer:
        resp = await run_agent_loop(req)

    assert resp.status == "completed"
    assert resp.result.type == "text"
    assert resp.result.text == "Fire watch is required for hot work."
    mocked_tool.assert_awaited_once()
    assert mocked_tool.call_args.kwargs["top_k"] == 3
    mocked_infer.assert_awaited_once()


@pytest.mark.asyncio
async def test_document_generation_flow_calls_tool_only_no_qwen():
    req = ExecuteTaskRequest(
        task_id="c3",
        prompt="generate a docx approval note for vessel V-101 wall loss finding",
        file_base64=None,
        file_mime_type=None,
    )
    tool_result = {"file_url": "/files/abc123-approval-note.docx", "file_name": "abc123-approval-note.docx"}

    with patch("executor.loop.generate_file", new=AsyncMock(return_value=tool_result)) as mocked_tool, \
         patch("executor.loop.call_inference", new=AsyncMock()) as mocked_infer:
        resp = await run_agent_loop(req)

    assert resp.status == "completed"
    assert resp.result.type == "file"
    assert resp.result.file_url == "/files/abc123-approval-note.docx"
    assert resp.result.file_name == "abc123-approval-note.docx"
    assert resp.result.text is None
    mocked_tool.assert_awaited_once()
    assert mocked_tool.call_args.kwargs["file_type"] == "docx"
    # Qwen must NOT be used to merely describe the file — no model call at all.
    mocked_infer.assert_not_awaited()


@pytest.mark.asyncio
async def test_tool_failure_retries_then_finalizes_with_error():
    req = ExecuteTaskRequest(
        task_id="c4",
        prompt="calculate 5 * 5",
        file_base64=None,
        file_mime_type=None,
    )
    with patch(
        "executor.loop.execute_code", new=AsyncMock(side_effect=RuntimeError("docker unavailable"))
    ) as mocked_tool:
        resp = await run_agent_loop(req)

    assert resp.status == "failed"
    assert "docker unavailable" in resp.error
    # MAX_TOOL_ATTEMPTS = 2 -> one retry, so exactly 2 calls total
    assert mocked_tool.await_count == 2


@pytest.mark.asyncio
async def test_tool_retry_succeeds_on_second_attempt():
    req = ExecuteTaskRequest(
        task_id="c5",
        prompt="search the manual for CUI inspection rules",
        file_base64=None,
        file_mime_type=None,
    )
    with patch(
        "executor.loop.search_docs",
        new=AsyncMock(side_effect=[RuntimeError("timeout"), {"results": []}]),
    ) as mocked_tool, patch(
        "executor.loop.call_inference", new=AsyncMock(return_value="No CUI info found.")
    ):
        resp = await run_agent_loop(req)

    assert resp.status == "completed"
    assert mocked_tool.await_count == 2
