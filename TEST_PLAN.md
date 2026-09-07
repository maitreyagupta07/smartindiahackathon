# Rigorous Test Plan — Sovereign On-Premise Agentic AI Workbench

Checklist form so it can be run top-to-bottom before a demo, or handed to
a teammate. Organized by capability the problem statement actually asks
for, not by file — each section says what it's proving, not just "does it
run." Check items off as you verify them; note the actual output next to
anything surprising.

Prereqs before starting: `./run.sh` running, Ollama + Docker both up,
`docker pull python:3.11-slim` done once.

---

## 1. Routing / classification — does the right task_type get picked?

Send each prompt with **no file attached** and confirm `task_type` in the
response (or `[ROUTER]` log line) matches, before checking output quality.

| # | Prompt | Expected task_type | Why this one |
|---|---|---|---|
| 1.1 | `tell me a joke` | text-generation | plain baseline |
| 1.2 | `execute this code: print(2+2)` | code-execution | explicit code-execution phrase |
| 1.3 | `calculate the flow rate` | text-generation | **must NOT** be code-execution — bare "calculate" is weak/ambiguous |
| 1.4 | `calculate 15 * 4` | code-execution | arithmetic expression is a strong signal, overrides the weak "calculate" |
| 1.5 | `calculate the sum: \`\`\`python\nprint(2+2)\n\`\`\`` | code-execution | code fence is a very strong signal |
| 1.6 | `search the SOP for hot work permit rules` | doc-search | explicit search phrase + domain term |
| 1.7 | `find the procedure for confined space entry` | doc-search | "find the procedure" contextual phrase |
| 1.8 | `make an approval note for the pump inspection` | text-generation | approval-note content with **no file format named** → stays text |
| 1.9 | `generate a Word document for the approval note for leak in vessel B21` | document-generation | explicit file format |
| 1.10 | `make a word doc of approval note of leak in vessel B2` | document-generation | file-format phrase at the FRONT of the sentence — the exact bug that was fixed |
| 1.11 | `Search the inspection reports for the leak in vessel B21, summarize the findings, and prepare an approval note as a Word document` | document-generation, `is_multi_step=True`, `workflow=["doc-search","document-generation"]` | multi-step intent |
| 1.12 | `make an excel sheet of pump readings` | document-generation | non-approval-note file format |
| 1.13 | `make a powerpoint about safety basics` | document-generation | pptx format |
| 1.14 | *(attach any image)* + `what is this` | vision | image mime type always wins, deterministic |
| 1.15 | *(attach image)* + `generate a word document from this` | vision, **not** document-generation | image must override conflicting text signals |
| 1.16 | *(inside a chat, chat_id set)* `what's the boiling point of water` | chat | plain question inside chat → KB/conversation mode |
| 1.17 | *(inside a chat, chat_id set)* `generate a word doc of oil leak in b23` | document-generation, **not** chat | actionable request inside chat must NOT be swallowed into KB-search mode — this was a real reported bug |
| 1.18 | *(inside a chat)* attach an image + `what color is this` | vision, **not** chat | image inside chat still routes to Moondream |

---

## 2. Text-generation

- [ ] 2.1 Plain question, no domain jargon — sane, on-topic answer.
- [ ] 2.2 Approval-note-flavored request (no file format) — uses `approval-note-lora` (check `model_used`), plain-text output matches the trained free-text format (Subject/Findings/Recommendation/Approval-style fields), **no literal `**` markdown asterisks**.
- [ ] 2.3 Same approval-note prompt fired twice — content differs (normal model variance) but the **structure/fields** stay consistent both times.
- [ ] 2.4 A refinery/industrial-jargon question with no actionable signal — stays text-generation, coherent answer.

## 3. Vision

- [ ] 3.1 Plain image + "what is in this photo" — single Moondream call, no reasoning chain (`needs_reasoning=False`).
- [ ] 3.2 Plain image + "explain what's wrong in this inspection photo" — chains Moondream → Qwen reasoning (`needs_reasoning=True`), two model calls in the log.
- [ ] 3.3 A real (or synthetic) engineering-drawing-style image — sanity-check the description is plausible, not garbage.
- [ ] 3.4 Unsupported/corrupt image bytes — fails cleanly with a populated `error`, not a 500 crash.

## 4. Document generation (Word / Excel / PPT)

Combinatorial — test the cells that matter, not just one happy path:

| Content type | Format | PDF attached? | Expect |
|---|---|---|---|
| Approval note | docx | no | LoRA adapter, generic-but-structured content, downloadable file |
| Approval note | docx | yes, text-layer PDF | content grounded in the PDF's actual facts (specific names/values from the PDF appear in the output) |
| Approval note | docx | yes, **scanned** (image-only) PDF | OCR path triggers (`[PDF_EXTRACT] ... trying OCR`), content still grounded correctly |
| Approval note | xlsx / pptx | no | still produces the requested format (not forced to docx) |
| Non-approval-note content (e.g. "quarterly sales trends") | pptx | no | uses base `TEXT_MODEL`, **not** the LoRA adapter |
| Computation-flavored ("first 20 Fibonacci numbers and their average") | xlsx | no | runs verification code via the sandbox first, file content matches the *verified* numbers, not model arithmetic |
| Multi-step ("search reports for X, summarize, prepare as Word doc") | docx | no | classified as document-generation; content is at least coherent (full doc-search-feeding-into-content-prep chaining is a known, flagged gap — see §11) |

For every row above:
- [ ] `result.type == "file"`, `file_url` present.
- [ ] File actually downloads via `GET /files/<name>` (not 404).
- [ ] Opened file (docx/xlsx/pptx) is a valid, non-corrupt Office file.
- [ ] No literal `**`/`__` markdown characters anywhere in the file text.

## 5. Code execution (real Docker sandbox)

- [ ] 5.1 Simple arithmetic (`execute this code: print(sum(range(1,101)))`) — correct computed answer (5050), not a guess.
- [ ] 5.2 A request needing an actual algorithm (e.g. "write and run code to check if 97 is prime") — Qwen generates real code, sandbox runs it, correct answer.
- [ ] 5.3 Code that intentionally errors (e.g. divide by zero) — `exit_code != 0`, the final answer honestly reports the failure, does **not** invent a plausible-looking result.
- [ ] 5.4 A request with no real code angle sent as code-execution by mistake — Qwen's codegen should still try; if it can't produce valid Python, the task fails cleanly (`CodeGenerationError` → `status: failed`), never silently runs the raw prompt as code.
- [ ] 5.5 **Network isolation**: from inside a running sandbox container (if you can get a shell into one, or by asking it to run `import urllib.request; urllib.request.urlopen('http://example.com')`), confirm the network call fails — proves `network_disabled=True` is real, not just configured.
- [ ] 5.6 **Resource limits**: an intentional infinite loop / runaway allocation — confirm it's killed at the timeout (~15s) rather than hanging the whole app.
- [ ] 5.7 Stop the Docker daemon (`sudo systemctl stop docker`), then submit a code task — confirm graceful degradation: `exit_code: 127`, a clear "sandbox unavailable" note, task still completes (doesn't 500). Restart Docker after.

## 6. Document search (general corpus + chat-scoped Knowledge Base)

- [ ] 6.1 Query that matches something in `tools/docs_corpus/` — relevant passage(s) returned, answer cites/uses them.
- [ ] 6.2 Query with no matching content — model says so honestly ("no information found"), doesn't fabricate an answer.
- [ ] 6.3 Upload a PDF to a chat's KB, then ask about it in that same chat — retrieves and answers from it correctly.
- [ ] 6.4 Upload a **scanned** PDF to a chat's KB — OCR path used, still ingests and is searchable.
- [ ] 6.5 Upload a `.docx`, `.pptx`, `.xlsx`, and `.txt`/`.md` file each — all four ingest successfully and are individually retrievable.
- [ ] 6.6 Try uploading an image to the KB upload endpoint — rejected with a clear message pointing to attaching it to a message instead (not a generic 500).
- [ ] 6.7 **Chat isolation**: upload different documents to two different `chat_id`s, ask the same question in both — each chat only ever sees its own document, never the other's.
- [ ] 6.8 Admin → Knowledge Base listing shows all uploaded documents across chats with correct chat titles.

## 7. Chat / conversational flow

- [ ] 7.1 Multi-turn conversation with a follow-up using a pronoun ("why would they benefit from it?") — correctly resolved against the recent conversation history.
- [ ] 7.2 A chat with zero prior history — still answers the first question sensibly (no crash on empty history).
- [ ] 7.3 `sources` field in the response is populated with `{filename, page, score}` when the KB was actually used, and is `null`/absent when it wasn't.
- [ ] 7.4 Resume a chat via `GET /api/chat/{chat_id}` — full message history and document list come back correctly.

## 8. Concurrency (the single most demo-breaking thing to skip)

- [ ] 8.1 Fire two plain text-generation tasks at the literal same time — their `started_at`/`completed_at` windows genuinely overlap (not serialized).
- [ ] 8.2 Fire two vision tasks at the same time — the **second one queues** behind the first (they should NOT overlap) — proves the smaller vision concurrency lane is real, not decorative.
- [ ] 8.3 Fire one text task and one vision task simultaneously — they run in their own lanes independently (text doesn't wait on vision or vice versa).
- [ ] 8.4 Fire more tasks than `max_concurrent_tasks` (currently 2) at once — extras genuinely queue (`status: "queued"` visible via polling) rather than erroring or being dropped.
- [ ] 8.5 One task errors (e.g. a bad code-execution request) while another is mid-flight — the failure doesn't affect the other task's result.

## 9. File serving / downloads

- [ ] 9.1 Every file type generated (docx/xlsx/pptx) is downloadable via `GET /files/<name>` with correct content and a sane `Content-Type`.
- [ ] 9.2 A nonexistent filename under `/files/` returns 404, not a crash.
- [ ] 9.3 Confirm files land in the single shared `shared_files/` directory (not scattered across stale per-service directories from the old architecture).

## 10. Sovereignty / air-gap proof (the actual selling point)

- [ ] 10.1 With no internet access at all (physically disconnect, or firewall-block outbound), run a full text/vision/doc-search/document-generation/code-execution cycle — all succeed (after the one-time ONNX embedding-model download has already happened once — see the caveat below).
- [ ] 10.2 Watch server logs during a doc-search call — zero telemetry lines (`Failed to send telemetry event...` should NEVER appear again after the fix).
- [ ] 10.3 Confirm `ollama` is unreachable via the LAN IP (`curl http://<lan-ip>:11434/api/tags` should time out) but works via `localhost`.
- [ ] 10.4 Confirm ports 8001 and 8002 are closed (`curl http://127.0.0.1:8001/` → connection refused) — only 8000 is ever listening.
- [ ] 10.5 **Known, documented, non-blocking gap**: ChromaDB's embedding model (`all-MiniLM-L6-v2`, ~79MB) downloads from the internet the very first time doc-search ever runs on a fresh machine. Pre-download/cache it (`~/.cache/chroma/onnx_models/`) before a truly offline demo.

## 11. Known limitations to demo around (don't get caught by these live)

- [ ] 11.1 The classifier's multi-step `workflow` metadata (e.g. `["doc-search","document-generation"]`) is exposed for the planner but **the planner does not yet actually chain doc-search results into document-generation content** — a "search X and generate a doc" request classifies correctly but the doc content isn't grounded in the search results yet. Don't promise this live without checking current state first.
- [ ] 11.2 Small-model unreliability: Qwen/the LoRA adapter occasionally don't perfectly follow grounding instructions (may answer from general knowledge instead of retrieved passages, or invent a plausible date/name in a generated document). This is model behavior, not a plumbing bug — retrieval itself has been verified correct. Re-running the same prompt often gives a cleaner result.
- [ ] 11.3 Handwritten-note OCR is not implemented — only printed-text OCR (Tesseract) is wired up. Don't demo a handwriting scan.

## 12. Multi-user / LAN access (the actual demo scenario)

- [ ] 12.1 Confirm `hostname -I`'s LAN IP is reachable from a second device on the same hotspot: `curl http://<lan-ip>:8000/health` → `{"status":"ok"}`.
- [ ] 12.2 Full end-to-end task submitted from the second device's browser, not curl — confirms the actual UI works cross-device, not just the API.
- [ ] 12.3 Two different devices submitting tasks at the same time — same concurrency guarantees as §8, now proven across real separate clients, not just two `asyncio.gather`'d calls from one script.

---

## Quick smoke-test script

For a fast pre-demo sanity pass (not a replacement for the above, just the
critical path in under 2 minutes):

```bash
curl -s http://localhost:8000/health
curl -s -X POST http://localhost:8000/api/submit-task -H "Content-Type: application/json" \
  -d '{"user_id":"smoke","prompt":"say hi back"}'
# then poll /api/task-status/<id> until completed
```

Automated coverage (`pytest app/tests/`) already covers routing logic,
tool orchestration, chat flow, and the shape/concurrency checks against a
live app — run it before every demo: `PYTHONPATH=. pytest app/tests/ -q`
should read `53 passed`.
