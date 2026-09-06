# Person A — working notes / ideas backlog

Not part of the contract — just a scratch list so nothing discussed gets lost.

## In progress
- [ ] PDF / scanned-report ingestion for approval-note requests (started — see below)

## Done — single-node deployment refactor (2026-09-07)
Collapsed the old 3-process deployment (backend:8000, agent:8002, tools:8001,
each an independent FastAPI app talking to the others over localhost HTTP)
into ONE FastAPI app (`app/`), so ONLY :8000 is ever a network listener.
Ollama (a separate local OS process) and the Docker code-execution sandbox
(a separate, network-disabled, resource-capped container per run) are the
only two things that intentionally remain outside the single process — one
because it's a third-party binary, the other because the container boundary
IS the actual security isolation. See app/main.py's docstring for the full
rationale. Old `agent/`, `tools/app/`, `backend/` directories are gone;
everything moved into `app/{api,agent,router,inference,tools,schemas,
storage,audit,tests}/` as direct Python calls instead of HTTP hops
(app/tools/facade.py replaces the old tools_client.py HTTP client).

Two real, pre-existing sovereignty gaps were found and only partially fixed
while validating this (neither was introduced by this refactor — they
existed in the multi-service version too, just never surfaced/tested):
- **ChromaDB telemetry** (fixed): its default client tries to send
  `CollectionAddEvent`/`CollectionQueryEvent` telemetry to Chroma's own
  servers on every doc-search call. Now disabled via
  `Settings(anonymized_telemetry=False)` in `app/tools/docsearch.py`.
- **ChromaDB's embedding model auto-download** (flagged, NOT fixed): the
  `ONNXMiniLM_L6_V2` embedding function downloads its ~79MB model weights
  from the internet the first time doc-search ever runs on a machine,
  caching to `~/.cache/chroma/onnx_models/`. For a genuinely air-gapped
  demo/deployment, that model needs to be pre-downloaded and cached BEFORE
  going offline — it is a real, one-time external dependency that no code
  change here removes, only a deployment-step note. Test this by clearing
  the cache dir and confirming doc-search works OFFLINE only if pre-cached.
- **Ollama's systemd unit bound `0.0.0.0`** (needs the user to run, sudo
  required): changed the plan to bind `127.0.0.1` only via
  `/etc/systemd/system/ollama.service.d/override.conf` + `daemon-reload` +
  `restart ollama` — commands given to the user to run themselves since
  this session has no sudo password. `config.json`'s `inference_host` was
  also changed from a LAN IP (needed for the old multi-machine testing
  setup) to `"localhost"`, since Ollama is now always co-located with the
  app on the same machine.
- **Docker sandbox not verified in this session** — Docker isn't installed/
  reachable in this dev sandbox, so code-execution could only be confirmed
  to fail *cleanly* (proper `status:"failed"` + populated `error`), not to
  actually succeed end-to-end. Needs a real run on a machine with Docker.

## Resolved — classifier rewrite (2026-09-06)
The "ambiguous phrasing confuses the docx content" concern below turned out
to be a real, specific bug: `_strip_file_format_phrase()` cut from the FIRST
matched file-format keyword to the END of the string, which only works when
that phrase trails the sentence. "make a word doc **of** approval note of
leak in vessel B2" has it up front, so stripping "cut to the end" threw away
almost the whole prompt. Fixed by removing only the matched phrase itself
(longest-match-first, since FILE_FORMAT_KEYWORDS has overlapping entries
like "word doc" inside "word document") wherever it sits, not everything
after it. Also rewrote router/classifier.py from flat substring keyword
matching to weighted multi-signal scoring (phrase signals + weak/strong word
tiers + a confidence threshold before accepting any non-default
classification + multi-step/workflow detection) — see its module docstring
for the full design. Verified via `agent/tests/test_classifier_signals.py`
(24 new tests) plus live runs of both the bug's exact repro prompt and the
two priority prompts from that task. Still true and worth re-checking
periodically: LoRA docx *content* itself remains stochastic (dates/specifics
vary or show as placeholders run to run) — that's the model, not routing,
and is a separate, already-known concern (see PERSON_A_NOTES.md history).

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
