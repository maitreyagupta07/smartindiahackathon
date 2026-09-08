"""
"Never hand the user a hallucinated number" guarantees.

Any request that asks for a computed value / sequence must have that value
produced by a real Python script run in the sandbox — never answered from
the model's own head, and never written into a generated file unless it was
sandbox-verified first.

  1. Routing: a bare "give me the first N Fibonacci numbers" goes to the
     code-execution flow; the same ask that also names a file format goes to
     document-generation (which runs its own verify-in-sandbox step).
  2. document-generation: if the sandbox can't verify the computation, the
     task FAILS loudly — it never falls through to a stage that would let
     the model supply the numbers.
  3. plain code-execution: a sandbox-unavailable run is reported honestly,
     never presented as a computed answer.

All model/tool calls are mocked — no Ollama, no Docker.
"""
import pytest
from unittest.mock import patch, AsyncMock

from app.schemas.task import ExecuteTaskRequest
from app.agent.loop import run_agent_loop
from app.router.classifier import classify_task


# --------------------------------------------------------------------------
# 1. Routing
# --------------------------------------------------------------------------

@pytest.mark.parametrize("prompt", [
    "generate me the first 10 fibonacci numbers",
    "what is the 20th prime number",
    "list the first 50 prime numbers",
    "give me the sum of 1 to 100",
    "make a multiplication table for 7",
])
def test_bare_numeric_requests_route_to_code_execution(prompt):
    assert classify_task(prompt, None) == "code-execution"


@pytest.mark.parametrize("prompt", [
    "generate me an excel file containing first 10 fibonacci numbers",
    "write a word doc with the first 15 fibonacci numbers",
    "make an xlsx of the first 20 prime numbers",
])
def test_numeric_requests_naming_a_file_stay_document_generation(prompt):
    assert classify_task(prompt, None) == "document-generation"


@pytest.mark.parametrize("prompt", [
    "please calculate the flow rate",
    "calculate the corrosion rate for this pipeline",
    "can you calculate the safety margin here",
])
def test_vague_calculate_wording_is_not_dragged_into_code_execution(prompt):
    assert classify_task(prompt, None) == "text-generation"


# --------------------------------------------------------------------------
# 2. document-generation: unverifiable computation fails loudly
# --------------------------------------------------------------------------

_SANDBOX_DOWN = {
    "stdout": "",
    "stderr": "(execution skipped: sandbox unavailable on this host)",
    "exit_code": 127,
}
_GEN_CODE = "import json\nprint(json.dumps({'fib': [0, 1, 1]}))"


@pytest.mark.asyncio
async def test_filegen_computation_fails_when_sandbox_never_verifies():
    req = ExecuteTaskRequest(
        task_id="vc1",
        prompt="Generate an xlsx file with the first 10 Fibonacci numbers",
    )
    with patch("app.agent.loop.execute_code", new=AsyncMock(return_value=_SANDBOX_DOWN)) as mocked_exec, \
         patch("app.agent.loop.generate_file", new=AsyncMock()) as mocked_gen, \
         patch("app.agent.loop.call_inference", new=AsyncMock(return_value=_GEN_CODE)):
        resp = await run_agent_loop(req)

    assert resp.status == "failed"
    assert resp.result.file_url is None
    # It regenerated the verification code once, then gave up — no third try.
    assert mocked_exec.await_count == 2
    # It NEVER produced a file with unverified numbers.
    mocked_gen.assert_not_awaited()
    # The error is actionable and names the real cause.
    assert "verified" in resp.error.lower()
    assert "docker" in resp.error.lower() or "sandbox" in resp.error.lower()


@pytest.mark.asyncio
async def test_filegen_computation_recovers_if_a_retry_verifies():
    req = ExecuteTaskRequest(
        task_id="vc2",
        prompt="Generate an xlsx file with the first 3 Fibonacci numbers",
    )
    good_run = {"stdout": '{"fib": [0, 1, 1]}\n', "stderr": "", "exit_code": 0}
    file_result = {"file_url": "/files/fib.xlsx", "file_name": "fib.xlsx"}

    with patch("app.agent.loop.execute_code",
               new=AsyncMock(side_effect=[_SANDBOX_DOWN, good_run])) as mocked_exec, \
         patch("app.agent.loop.generate_file", new=AsyncMock(return_value=file_result)) as mocked_gen, \
         patch("app.agent.loop.call_inference", new=AsyncMock(return_value=_GEN_CODE)):
        resp = await run_agent_loop(req)

    assert resp.status == "completed"
    assert resp.result.type == "file"
    assert resp.result.file_url == "/files/fib.xlsx"
    assert mocked_exec.await_count == 2
    # File content came straight from the verified stdout.
    content = mocked_gen.call_args.kwargs["content"]
    assert content["sections"] == [{"heading": "Fib", "body": "0\n1\n1"}]


# --------------------------------------------------------------------------
# 3. plain code-execution: sandbox-unavailable is reported, not answered
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_code_execution_reports_sandbox_unavailable_honestly():
    req = ExecuteTaskRequest(task_id="vc3", prompt="list the first 10 fibonacci numbers")

    # 1st call_inference -> the generated Python; 2nd -> the explanation the
    # model writes from the (failed) run.
    infer = AsyncMock(side_effect=[_GEN_CODE, "The sandbox is not available, so I could not run and verify this."])
    with patch("app.agent.loop.execute_code", new=AsyncMock(return_value=_SANDBOX_DOWN)), \
         patch("app.agent.loop.call_inference", new=infer):
        resp = await run_agent_loop(req)

    assert resp.status == "completed"  # honest report, not a hard failure
    # The prompt handed to the explaining call tells the model the code was
    # NOT executed and forbids guessing the answer.
    second_prompt = infer.call_args_list[1].kwargs.get("prompt") or infer.call_args_list[1].args[1]
    assert "not available" in second_prompt.lower()
    assert "not executed" in second_prompt.lower() or "was not" in second_prompt.lower()
    assert "do not" in second_prompt.lower()
