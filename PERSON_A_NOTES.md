# Person A — working notes / ideas backlog

Not part of the contract — just a scratch list so nothing discussed gets lost.

## In progress
- [ ] PDF / scanned-report ingestion for approval-note requests (started — see below)

## Ideas not yet started
- [ ] Second small LoRA adapter for another recurring document type (e.g. shift
      handover note, maintenance work order) — reuse the existing Unsloth ->
      checkpoint -> convert_lora_to_gguf.py pipeline built for approval-note-lora.
- [ ] Small eval/regression script for the adapter: run a fixed set of 5-10
      sample findings through Ollama and check the output always has the core
      approval-note fields (Subject/Findings/Recommendation/Approval Status).
      Cheap insurance against silent model drift (the kind of off-distribution
      JSON bug found and fixed on 2026-09-06).
- [ ] One-command health-check/bootstrap script for demo day: verifies Ollama
      is up, both models (qwen2.5:1.5b-instruct, approval-note-lora) are
      registered, and config.json's ports are reachable.

## Known bugs NOT owned by Person A (flagged, not fixed)
- Person F's code-execution flow: `test_code_execution_flow_calls_tool_then_qwen`
  in agent/tests/test_tools_integration.py fails. Person F's planner.py has
  Qwen generate Python code before execution now (CODEEXEC_CODE_MARKER /
  `_is_usable_python`), and that test mocks Qwen's response as the plain
  string "The result is 4." (not real code). Person F's own validation
  correctly rejects that as unusable Python and raises `CodeGenerationError`,
  so the test fails — the test's mock needs updating to return an actual
  Python snippet. Pre-existing, unrelated to any Person-A change; flag to
  Person F rather than fix directly (not our code/track).

## Design rule for PDF/scanned-report ingestion
Extracted PDF/scan text should ONLY be fed into the approval-note-lora
adapter when the request is already an approval-note request
(`is_approval_note_request(state.prompt)` — the same check already used to
route to the LoRA elsewhere). A PDF uploaded alongside an unrelated request
(doc-search, plain text-generation, etc.) must not be force-fed into the
adapter — it's trained for one narrow job only.
