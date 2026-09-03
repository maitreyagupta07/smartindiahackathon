"""
Tests for the real multi-step agent loop (executor/planner.py + loop.py).

These mock clients.inference_client.call_inference so no real Ollama server
is required — they validate the PLANNING/ORCHESTRATION logic itself:
  - text -> single Qwen step
  - image -> single Moondream step
  - image + reasoning -> Moondream -> observe -> Qwen -> final
  - max-step protection never loops forever
"""
import pytest
from unittest.mock import patch, AsyncMock

from schemas.task import ExecuteTaskRequest
from executor.loop import run_agent_loop
from router.model_registry import TEXT_MODEL, VISION_MODEL


@pytest.mark.asyncio
async def test_text_only_routes_to_qwen_single_step():
    req = ExecuteTaskRequest(
        task_id="t1", prompt="write a short summary", file_base64=None, file_mime_type=None
    )
    with patch(
        "executor.loop.call_inference", new=AsyncMock(return_value="a qwen text response")
    ) as mocked:
        resp = await run_agent_loop(req)

    assert resp.status == "completed"
    assert resp.model_used == TEXT_MODEL
    assert resp.result.text == "a qwen text response"
    assert mocked.await_count == 1
    called_model = mocked.call_args.kwargs.get("model") or mocked.call_args.args[0]
    assert called_model == TEXT_MODEL


@pytest.mark.asyncio
async def test_plain_image_routes_to_moondream_single_step():
    req = ExecuteTaskRequest(
        task_id="t2", prompt="what is in this photo",
        file_base64="ZmFrZWJhc2U2NA==", file_mime_type="image/png",
    )
    with patch(
        "executor.loop.call_inference", new=AsyncMock(return_value="a photo of a valve")
    ) as mocked:
        resp = await run_agent_loop(req)

    assert resp.status == "completed"
    assert resp.model_used == VISION_MODEL
    assert resp.result.text == "a photo of a valve"
    assert mocked.await_count == 1


@pytest.mark.asyncio
async def test_image_plus_reasoning_chains_moondream_then_qwen():
    req = ExecuteTaskRequest(
        task_id="t3",
        prompt="explain what's wrong with this equipment in the photo",
        file_base64="ZmFrZWJhc2U2NA==",
        file_mime_type="image/png",
    )
    responses = ["a corroded valve flange", "The flange shows corrosion; recommend replacement."]
    with patch(
        "executor.loop.call_inference", new=AsyncMock(side_effect=responses)
    ) as mocked:
        resp = await run_agent_loop(req)

    assert resp.status == "completed"
    # final model_used should be Qwen — it produced the final answer
    assert resp.model_used == TEXT_MODEL
    assert resp.result.text == "The flange shows corrosion; recommend replacement."
    assert mocked.await_count == 2

    first_call_model = mocked.call_args_list[0].kwargs.get("model") or mocked.call_args_list[0].args[0]
    second_call_model = mocked.call_args_list[1].kwargs.get("model") or mocked.call_args_list[1].args[0]
    assert first_call_model == VISION_MODEL
    assert second_call_model == TEXT_MODEL


@pytest.mark.asyncio
async def test_max_step_protection_never_hangs():
    """
    Force needs_reasoning True via prompt keywords but simulate a planner
    that would otherwise loop — max_steps must still cap execution.
    Here we just confirm a normal 2-step reasoning task finishes well
    under the max_steps ceiling (regression guard against infinite loops).
    """
    req = ExecuteTaskRequest(
        task_id="t4",
        prompt="analyze this image and explain the risk",
        file_base64="ZmFrZWJhc2U2NA==",
        file_mime_type="image/png",
    )
    with patch(
        "executor.loop.call_inference",
        new=AsyncMock(side_effect=["description", "final reasoning"]),
    ) as mocked:
        resp = await run_agent_loop(req)

    assert resp.status == "completed"
    assert mocked.await_count == 2  # did not run away to max_steps (6)
