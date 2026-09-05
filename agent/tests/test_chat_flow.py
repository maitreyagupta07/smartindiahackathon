"""
Tests for the chat flow: chat-scoped Knowledge Base retrieval +
conversational context, and per-chat isolation.

Mocks clients.tools_client.search_docs and clients.inference_client.call_inference
so no real Tools / Ollama services are needed — these validate the
orchestration: when chat_id is set the loop must retrieve with that chat_id,
fold recent conversation into the Qwen prompt, and surface sources.
"""
import pytest
from unittest.mock import patch, AsyncMock

from schemas.task import ExecuteTaskRequest
from executor.loop import run_agent_loop
from router.model_registry import TEXT_MODEL


@pytest.mark.asyncio
async def test_chat_flow_retrieves_scoped_docs_and_uses_history():
    req = ExecuteTaskRequest(
        task_id="chat1",
        prompt="Why would they benefit from it?",
        chat_id="abc123",
        history=[
            {"role": "user", "content": "What is this research paper about?"},
            {"role": "assistant", "content": "It proposes a new inspection scheduling system."},
            {"role": "user", "content": "Who are the target users?"},
            {"role": "assistant", "content": "Plant reliability engineers."},
        ],
    )
    tool_result = {
        "results": [
            {"text": "Engineers cut planning time by 40%.", "source": "research.pdf", "page": 7, "score": 0.88},
            {"text": "Fewer unplanned shutdowns.", "source": "research.pdf", "page": 9, "score": 0.81},
        ]
    }

    with patch("executor.loop.search_docs", new=AsyncMock(return_value=tool_result)) as mocked_tool, \
         patch("executor.loop.call_inference", new=AsyncMock(return_value="They save planning time and avoid shutdowns.")) as mocked_infer:
        resp = await run_agent_loop(req)

    assert resp.status == "completed"
    assert resp.task_type == "chat"
    assert resp.result.type == "text"
    assert resp.result.text == "They save planning time and avoid shutdowns."

    # Retrieval was chat-scoped.
    mocked_tool.assert_awaited_once()
    assert mocked_tool.call_args.kwargs["chat_id"] == "abc123"

    # The Qwen prompt carried BOTH the retrieved KB passages and the recent turns.
    sent_prompt = mocked_infer.call_args.kwargs.get("prompt") or mocked_infer.call_args.args[1]
    assert "research.pdf" in sent_prompt
    assert "page 7" in sent_prompt
    assert "Who are the target users?" in sent_prompt
    assert "Plant reliability engineers." in sent_prompt
    assert "Why would they benefit from it?" in sent_prompt

    # Sources are surfaced with filename + page.
    assert resp.result.sources == [
        {"filename": "research.pdf", "page": 7, "score": 0.88},
        {"filename": "research.pdf", "page": 9, "score": 0.81},
    ]


@pytest.mark.asyncio
async def test_chat_flow_without_history_still_answers():
    req = ExecuteTaskRequest(task_id="chat2", prompt="What is this document about?", chat_id="solo")
    tool_result = {"results": [{"text": "A vessel inspection report.", "source": "paper.pdf", "page": 1, "score": 0.9}]}

    with patch("executor.loop.search_docs", new=AsyncMock(return_value=tool_result)), \
         patch("executor.loop.call_inference", new=AsyncMock(return_value="It's a vessel inspection report.")) as mocked_infer:
        resp = await run_agent_loop(req)

    assert resp.status == "completed"
    assert resp.result.text == "It's a vessel inspection report."
    sent_prompt = mocked_infer.call_args.kwargs.get("prompt") or mocked_infer.call_args.args[1]
    assert "no earlier conversation" in sent_prompt.lower()


@pytest.mark.asyncio
async def test_chat_flow_no_matching_docs_is_honest():
    req = ExecuteTaskRequest(task_id="chat3", prompt="Anything about turbines?", chat_id="empty")

    with patch("executor.loop.search_docs", new=AsyncMock(return_value={"results": []})), \
         patch("executor.loop.call_inference", new=AsyncMock(return_value="No information on turbines in the uploaded documents.")) as mocked_infer:
        resp = await run_agent_loop(req)

    assert resp.status == "completed"
    assert resp.result.sources is None
    sent_prompt = mocked_infer.call_args.kwargs.get("prompt") or mocked_infer.call_args.args[1]
    assert "no relevant passages" in sent_prompt.lower()


@pytest.mark.asyncio
async def test_chat_isolation_each_chat_only_sees_its_own_chat_id():
    """Chat A and Chat B ask the identical question; each retrieval must carry
    only its own chat_id — the tools service filters on it, so a document
    uploaded in Chat A can never come back for Chat B."""
    captured = []

    async def fake_search(query, top_k=3, chat_id=None):
        captured.append(chat_id)
        payloads = {
            "chatA": {"results": [{"text": "A-only content", "source": "A.pdf", "page": 2, "score": 0.9}]},
            "chatB": {"results": [{"text": "B-only content", "source": "B.pdf", "page": 3, "score": 0.9}]},
        }
        return payloads.get(chat_id, {"results": []})

    with patch("executor.loop.search_docs", new=fake_search), \
         patch("executor.loop.call_inference", new=AsyncMock(return_value="ok")):
        ra = await run_agent_loop(ExecuteTaskRequest(task_id="a", prompt="What does the document say about X?", chat_id="chatA"))
        rb = await run_agent_loop(ExecuteTaskRequest(task_id="b", prompt="What does the document say about X?", chat_id="chatB"))

    assert captured == ["chatA", "chatB"]
    assert ra.result.sources == [{"filename": "A.pdf", "page": 2, "score": 0.9}]
    assert rb.result.sources == [{"filename": "B.pdf", "page": 3, "score": 0.9}]


@pytest.mark.asyncio
async def test_non_chat_request_is_unchanged():
    """No chat_id -> the loop must behave exactly as before (plain text flow)."""
    req = ExecuteTaskRequest(task_id="plain", prompt="write a short summary")
    with patch("executor.loop.call_inference", new=AsyncMock(return_value="a summary")) as mocked_infer:
        resp = await run_agent_loop(req)

    assert resp.status == "completed"
    assert resp.task_type == "text-generation"
    assert resp.result.text == "a summary"
    assert resp.result.sources is None
    mocked_infer.assert_awaited_once()
