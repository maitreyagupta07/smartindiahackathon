# Tools Service (Person C)

Implements contract §2.6 of MASTER_BUILD_GUIDE.md:
- `POST /tools/execute-code`  — sandboxed Docker code execution (network-disabled)
- `POST /tools/search-docs`   — local ChromaDB search over `docs_corpus/`
- `POST /tools/generate-file` — real .docx/.xlsx/.pptx generation, written to `FILES_DIR`

Runs on port 8001, **localhost only** — never exposed to the LAN (§2.2).
Only ever called by Person F's Agent Loop.

## Setup

```bash
pip install -r requirements.txt
```

Requires Docker running locally (for `/tools/execute-code`) and pulls the
`python:3.11-slim` image on first use:

```bash
docker pull python:3.11-slim
```

Copy `config.json.example` to the repo root as `config.json` if the team
hasn't already created one (see MASTER_BUILD_GUIDE §2.2). This service reads
`tools_port` / `ports.tools` and `FILES_DIR` from it — never hardcode either.

## Run

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8001
```
or
```bash
python -m app.main
```

## Test alone (per MASTER_BUILD_GUIDE, Person C's "Test alone with")

```bash
# 1. Code execution
curl -X POST http://localhost:8001/tools/execute-code \
  -H "Content-Type: application/json" \
  -d '{"code": "print(2 + 2)", "language": "python"}'

# 2. Document search
curl -X POST http://localhost:8001/tools/search-docs \
  -H "Content-Type: application/json" \
  -d '{"query": "permit to work hot work fire watch", "top_k": 3}'

# 3. File generation
curl -X POST http://localhost:8001/tools/generate-file \
  -H "Content-Type: application/json" \
  -d '{
        "type": "docx",
        "content": {
          "title": "Approval Note - Vessel V-101",
          "sections": [
            {"heading": "Finding", "body": "Thickness survey shows 12% wall loss."},
            {"heading": "Recommendation", "body": "Schedule repair before next turnaround."}
          ]
        }
      }'

# 4. Health check
curl http://localhost:8001/health
```

## Self-verification checklist (§2.10) — run before reporting "done"

1. Shape check: diff each response above against §2.6's exact JSON shapes.
2. Error-path check: POST malformed JSON / missing fields to each endpoint,
   confirm `{"error": "..."}` with 400.
3. No hardcoded ports/paths: confirm `app/config.py` is the only place
   reading `tools_port`/`FILES_DIR`, from `config.json` at repo root.
4. Isolation check: stop Docker, confirm `/tools/execute-code` returns a
   clean `{"error": ...}` (not a crash) while `/tools/search-docs` and
   `/tools/generate-file` still work fine — this service has no dependency
   on Person A, B, or F to run correctly on its own.
5. Self-report exactly which checks passed/failed.

## Notes for Person F (consumer of this service)

- `/tools/execute-code` only accepts code text — no file-attachment field.
  If a task needs to operate on uploaded data, embed it as text inside the
  `code` string (per §2.6's scope-limit note).
- `/tools/generate-file`'s `file_url` is always a path like
  `/files/8f3a1c-approval-note.docx`, never a `localhost:8001` URL — pass it
  through unchanged into your own `/execute-task` response (§2.7a).
