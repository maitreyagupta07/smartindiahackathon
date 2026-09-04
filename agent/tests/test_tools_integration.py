"""
Tests for Person C tool integration in the agent loop
(executor/planner.py tool selection + executor/loop.py dispatch).

Mocks clients.tools_client functions (and call_inference for the
qwen chaining steps) so no real Person C / Person A services are required —
these validate the ORCHESTRATION logic: which tool gets called, in what
order, with what args, and how tool failures are handled.
"""
import json

import pytest
from unittest.mock import patch, AsyncMock

from schemas.task import ExecuteTaskRequest
from executor.loop import run_agent_loop
from router.model_registry import TEXT_MODEL, LORA_ADAPTER


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
async def test_document_generation_flow_prepares_content_via_qwen_before_generate_file():
    """
    Person F must never hand Person C's generate_file the raw user prompt.
    A content-preparation Qwen call comes first and produces structured
    FileContent JSON, which is what actually gets passed to generate_file.
    """
    req = ExecuteTaskRequest(
        task_id="c3",
        prompt="generate a docx approval note for vessel V-101 wall loss finding",
        file_base64=None,
        file_mime_type=None,
    )
    prepared_content = {
        "title": "Approval Note: Vessel V-101 Wall Loss Finding",
        "sections": [
            {"heading": "Finding", "body": "Wall loss identified on vessel V-101."},
            {"heading": "Approval", "body": "Recommended for approval pending inspection."},
        ],
    }
    tool_result = {"file_url": "/files/abc123-approval-note.docx", "file_name": "abc123-approval-note.docx"}

    with patch("executor.loop.generate_file", new=AsyncMock(return_value=tool_result)) as mocked_tool, \
         patch("executor.loop.call_inference", new=AsyncMock(return_value=json.dumps(prepared_content))) as mocked_infer:
        resp = await run_agent_loop(req)

    assert resp.status == "completed"
    assert resp.result.type == "file"
    assert resp.result.file_url == "/files/abc123-approval-note.docx"
    assert resp.result.file_name == "abc123-approval-note.docx"
    assert resp.result.text is None

    # Qwen was used exactly once, for content-preparation, and never saw the
    # internal filegen stage marker.
    mocked_infer.assert_awaited_once()
    sent_prompt = mocked_infer.call_args.kwargs.get("prompt") or mocked_infer.call_args.args[1]
    assert "__FILEGEN_" not in sent_prompt

    mocked_tool.assert_awaited_once()
    assert mocked_tool.call_args.kwargs["file_type"] == "docx"
    # The raw user prompt must NOT be what lands in the file — the prepared
    # structured content must be used instead.
    assert mocked_tool.call_args.kwargs["content"] == prepared_content


@pytest.mark.asyncio
async def test_document_generation_flow_verifies_computation_before_content_prep():
    """
    Numerical/computational file requests must run execute_code first to get
    verified data, THEN prepare structured content grounded in that data —
    never trusting Qwen's own arithmetic or copying the prompt into the file.
    """
    req = ExecuteTaskRequest(
        task_id="c6",
        prompt="Generate an xlsx file with the first 3 Fibonacci numbers and their average",
        file_base64=None,
        file_mime_type=None,
    )
    generated_code = "import json\nprint(json.dumps({'fib': [0, 1, 1], 'average': 0.67}))"
    exec_result = {"stdout": "{\"fib\": [0, 1, 1], \"average\": 0.67}\n", "stderr": "", "exit_code": 0}
    prepared_content = {
        "title": "First 3 Fibonacci Numbers",
        "sections": [
            {"heading": "Values", "body": "Index 0: 0\nIndex 1: 1\nIndex 2: 1"},
            {"heading": "Summary", "body": "Average: 0.67"},
        ],
    }
    file_result = {"file_url": "/files/fib.xlsx", "file_name": "fib.xlsx"}

    with patch("executor.loop.execute_code", new=AsyncMock(return_value=exec_result)) as mocked_exec, \
         patch("executor.loop.generate_file", new=AsyncMock(return_value=file_result)) as mocked_gen, \
         patch(
             "executor.loop.call_inference",
             new=AsyncMock(side_effect=[generated_code, json.dumps(prepared_content)]),
         ) as mocked_infer:
        resp = await run_agent_loop(req)

    assert resp.status == "completed"
    assert resp.result.type == "file"
    assert resp.result.file_url == "/files/fib.xlsx"

    # Two Qwen calls: generate verification code, then prepare content.
    assert mocked_infer.await_count == 2
    # execute_code was called with the code Qwen generated, verifying the data.
    mocked_exec.assert_awaited_once()
    assert mocked_exec.call_args.kwargs["code"] == generated_code
    # generate_file received the prepared structured content, not the prompt.
    mocked_gen.assert_awaited_once()
    assert mocked_gen.call_args.kwargs["content"] == prepared_content
    assert mocked_gen.call_args.kwargs["file_type"] == "xlsx"


@pytest.mark.asyncio
async def test_approval_note_flow_uses_lora_adapter_for_content_prep():
    """
    Person A's fine-tuned approval-note-lora adapter must be used for the
    content-preparation Qwen call whenever the request is an approval note —
    not the base TEXT_MODEL, and not for other document-generation requests.
    """
    req = ExecuteTaskRequest(
        task_id="c7",
        prompt="Generate an approval note docx for vessel V-101 wall loss finding",
        file_base64=None,
        file_mime_type=None,
    )
    prepared_content = {
        "title": "Approval Note: Vessel V-101",
        "sections": [{"heading": "Finding", "body": "Wall loss identified on vessel V-101."}],
    }
    tool_result = {"file_url": "/files/approval.docx", "file_name": "approval.docx"}

    with patch("executor.loop.generate_file", new=AsyncMock(return_value=tool_result)), \
         patch("executor.loop.call_inference", new=AsyncMock(return_value=json.dumps(prepared_content))) as mocked_infer:
        resp = await run_agent_loop(req)

    assert resp.status == "completed"
    assert resp.model_used == LORA_ADAPTER
    mocked_infer.assert_awaited_once()
    assert mocked_infer.call_args.kwargs["model"] == LORA_ADAPTER


@pytest.mark.asyncio
async def test_non_approval_note_filegen_does_not_use_lora_adapter():
    req = ExecuteTaskRequest(
        task_id="c8",
        prompt="Generate a pptx presentation about quarterly sales trends",
        file_base64=None,
        file_mime_type=None,
    )
    prepared_content = {
        "title": "Quarterly Sales Summary",
        "sections": [{"heading": "Overview", "body": "Sales grew steadily this quarter."}],
    }
    tool_result = {"file_url": "/files/sales.pptx", "file_name": "sales.pptx"}

    with patch("executor.loop.generate_file", new=AsyncMock(return_value=tool_result)), \
         patch("executor.loop.call_inference", new=AsyncMock(return_value=json.dumps(prepared_content))) as mocked_infer:
        resp = await run_agent_loop(req)

    assert resp.status == "completed"
    assert resp.model_used == TEXT_MODEL
    mocked_infer.assert_awaited_once()
    assert mocked_infer.call_args.kwargs["model"] == TEXT_MODEL


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
