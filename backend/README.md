# Person B — API, Router & Task Queue

Implements §2.3 of MASTER_BUILD_GUIDE.md exactly. Port 8000, LAN-exposed.

## Run

```bash
pip install -r requirements.txt

# Terminal 1 — real thing once Person F is ready, or the mock for now:
python mock_executor.py        # stands in for Person F on port 8002

# Terminal 2:
python main.py                 # this service, port 8000, binds 0.0.0.0
```

## Test alone (per Part 3's "Test alone with")

```bash
# submit a task
curl -X POST http://localhost:8000/api/submit-task \
  -H "Content-Type: application/json" \
  -d '{"user_id":"u1","prompt":"hello","file_base64":null,"file_name":null,"file_mime_type":null}'

# poll status with the returned task_id
curl http://localhost:8000/api/task-status/<task_id>

# audit log
curl http://localhost:8000/api/audit-log

# concurrency check (§2.10.5) — fire two at the literal same moment:
(curl -s -X POST http://localhost:8000/api/submit-task -d '{"user_id":"u1","prompt":"a"}' -H "Content-Type: application/json" &
 curl -s -X POST http://localhost:8000/api/submit-task -d '{"user_id":"u2","prompt":"b"}' -H "Content-Type: application/json" &
 wait)
# then poll both task-status endpoints and confirm started_at/completed_at overlap
```

## §2.10 self-verification status (fill in as you actually run these — do not mark done without running)

- [ ] Shape check: submit-task, task-status, audit-log responses diffed field-by-field against §2.3
- [ ] Error-path check: malformed submit-task body → `400` + `{"error": "..."}`
- [ ] Task-failure check: kill the executor mid-flight → task ends up `status: "failed"` with populated `error`, still `200 OK` from task-status
- [ ] No hardcoded ports/paths: confirmed `main.py` reads `config.json`, doesn't hardcode 8000/8002/FILES_DIR
- [ ] Concurrency check: two simultaneous submits, overlapping `started_at`/`completed_at`
- [ ] Isolation check: with the executor unreachable, `submit-task` still returns `{"task_id","status":"queued"}` immediately, and the task later shows `status: "failed"` (not a crash)

## Not this role's job
No model calls, no tool calls, no reasoning — that's Person F. This service only receives, queues, tracks status, and serves files/frontend.
