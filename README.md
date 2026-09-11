# ERSA — On-Premise Agentic AI Workbench

Single-node deployment: one FastAPI app, one LAN-facing port (`:8000`).
See `PERSON_A_NOTES.md` at the repo root for the full audit/rationale
behind this architecture.

## Run

From the repo root:

```bash
./run.sh
```

This starts Ollama (if not already running), checks Docker is reachable,
sets up `.venv` on first run, and starts the app on `0.0.0.0:8000`.

Open `http://localhost:8000/` locally, or `http://<this-machine's-LAN-IP>:8000/`
from another device on the same network.

## Manual run

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
./.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## Layout

```
app/
  main.py       # the one FastAPI app / one network listener
  api/          # HTTP routes: tasks, chat, error handling, shared dispatch
  agent/        # plan -> act -> observe -> replan loop (executor)
  router/       # task classification + model selection
  inference/    # Ollama client (Ollama itself stays a separate local process)
  tools/        # code execution (Docker sandbox), doc search, file generation, PDF/OCR
  schemas/      # request/response models
  storage/      # shared config.json loader, FILES_DIR resolution
  audit/        # hash-chained audit log
  tests/        # pytest suite
```

Everything under `app/` runs as one Python process, calling each other
directly — EXCEPT Ollama (a separate local OS process, invoked over HTTP
to `localhost` only) and the Docker code-execution sandbox (a separate,
ephemeral, network-disabled container per run — that boundary is the
actual security isolation and is preserved exactly as before).

## Test

```bash
PYTHONPATH=. ./.venv/bin/pytest app/tests/
```

Most tests mock the model/tool calls and run in milliseconds.
`test_endpoint.py` and part of `test_concurrency.py` are real integration
tests that need Ollama actually running.
