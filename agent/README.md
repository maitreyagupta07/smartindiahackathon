# Agent Loop / Task Executor — Person F

Implements `POST /execute-task` per Master Build Guide contract §2.4.

## Structure
- `main.py` — FastAPI app entry point, port 8002
- `router/` — model routing decision (task_type + model selection)
- `executor/` — plan → act → observe → finalize loop
- `clients/` — HTTP clients for Person A (inference) and Person C (tools)
- `schemas/` — request/response models matching contract shapes exactly
- `tests/` — shape checks, router unit tests, concurrency proof

## Run locally
```bash
pip install -r requirements.txt
python main.py
```
Service starts on `http://0.0.0.0:8002` (localhost-only per §2.2 — not exposed to LAN by the team, host binding here is just for local dev).

## Test
```bash
pytest tests/
```

Run `tests/test_concurrency.py` again against the REAL Person A/C services once
wired in — this is the check most likely to silently regress once real
(slower, blocking-risk) HTTP calls replace the stubs.

## Contract reminders
- Do not rename/add/drop any field in `schemas/task.py` without going through
  the Contract Change Protocol (Master Build Guide §4.4).
- Must remain truly async end-to-end — no blocking calls anywhere in the
  `executor/loop.py` chain (§2.4 critical rule).
- `task_type` is this service's responsibility to set, every time, including
  on failure once known (§2.4).
