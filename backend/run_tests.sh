#!/usr/bin/env bash
# One-shot test runner for Person B's backend.
# Starts the mock executor (stand-in for Person F) + the real backend, runs every
# check from README.md / §2.10, prints PASS/FAIL for each, then shuts both down.
#
# Usage:
#   chmod +x run_tests.sh
#   ./run_tests.sh
#
# To test against the REAL Person F service instead of the mock, just don't start
# mock_executor.py yourself first — set SKIP_MOCK=1 and make sure your real
# executor is already running on port 8002:
#   SKIP_MOCK=1 ./run_tests.sh

set -uo pipefail
cd "$(dirname "$0")"

PASS=0
FAIL=0
pass() { echo "  PASS: $1"; PASS=$((PASS+1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL+1)); }

cleanup() {
  echo
  echo "Shutting down..."
  [ -n "${MOCK_PID:-}" ] && kill -9 "$MOCK_PID" 2>/dev/null
  [ -n "${MAIN_PID:-}" ] && kill -9 "$MAIN_PID" 2>/dev/null
  rm -f audit_log.sqlite3
}
trap cleanup EXIT

rm -f audit_log.sqlite3

if [ -z "${SKIP_MOCK:-}" ]; then
  echo "Starting mock executor (Person F stand-in) on :8002..."
  python3 mock_executor.py > /tmp/mock.log 2>&1 &
  MOCK_PID=$!
else
  echo "SKIP_MOCK=1 set — assuming a real executor is already running on :8002"
fi

echo "Starting Person B backend on :8000..."
python3 main.py > /tmp/main.log 2>&1 &
MAIN_PID=$!
sleep 2

if ! curl -s -o /dev/null -w "" http://localhost:8000/api/audit-log; then
  echo "Backend did not come up — check /tmp/main.log"
  cat /tmp/main.log
  exit 1
fi

echo
echo "=== 1. Shape check: submit-task ==="
RESP=$(curl -s -X POST http://localhost:8000/api/submit-task -H "Content-Type: application/json" \
  -d '{"user_id":"u1","prompt":"hello","file_base64":null,"file_name":null,"file_mime_type":null}')
echo "  response: $RESP"
TID=$(echo "$RESP" | python3 -c "import json,sys;print(json.load(sys.stdin).get('task_id',''))" 2>/dev/null)
STATUS=$(echo "$RESP" | python3 -c "import json,sys;print(json.load(sys.stdin).get('status',''))" 2>/dev/null)
if [ -n "$TID" ] && [ "$STATUS" = "queued" ]; then pass "submit-task returns task_id + status:queued"; else fail "submit-task shape wrong"; fi

echo
echo "=== 2. Shape check: task-status (poll until done) ==="
FINAL=""
for i in $(seq 1 10); do
  FINAL=$(curl -s http://localhost:8000/api/task-status/"$TID")
  ST=$(echo "$FINAL" | python3 -c "import json,sys;print(json.load(sys.stdin).get('status',''))" 2>/dev/null)
  [ "$ST" = "completed" ] || [ "$ST" = "failed" ] && break
  sleep 1
done
echo "  final: $FINAL"
if [ "$ST" = "completed" ]; then pass "task reached completed"; else fail "task never completed (status=$ST)"; fi

echo
echo "=== 3. Shape check: audit-log ==="
AUDIT=$(curl -s http://localhost:8000/api/audit-log)
echo "  $AUDIT"
echo "$AUDIT" | python3 -c "import json,sys; d=json.load(sys.stdin); assert 'entries' in d and len(d['entries'])>=1" \
  && pass "audit-log has entries" || fail "audit-log missing/empty"

echo
echo "=== 4. Error-path check: malformed JSON ==="
CODE=$(curl -s -o /tmp/err1.json -w "%{http_code}" -X POST http://localhost:8000/api/submit-task \
  -H "Content-Type: application/json" -d 'not valid json')
BODY=$(cat /tmp/err1.json)
echo "  http=$CODE body=$BODY"
if [ "$CODE" = "400" ] && echo "$BODY" | grep -q '"error"'; then pass "malformed request -> 400 + {error}"; else fail "malformed request wrong shape (got $CODE)"; fi

echo
echo "=== 5. Error-path check: missing task ==="
CODE=$(curl -s -o /tmp/err2.json -w "%{http_code}" http://localhost:8000/api/task-status/does-not-exist)
BODY=$(cat /tmp/err2.json)
echo "  http=$CODE body=$BODY"
if [ "$CODE" = "404" ] && echo "$BODY" | grep -q '"error"'; then pass "missing task -> 404 + {error}"; else fail "missing task wrong shape (got $CODE)"; fi

echo
echo "=== 6. Concurrency check: two simultaneous submits ==="
curl -s -X POST http://localhost:8000/api/submit-task -H "Content-Type: application/json" \
  -d '{"user_id":"cA","prompt":"a"}' > /tmp/c1.json &
CURL1_PID=$!
curl -s -X POST http://localhost:8000/api/submit-task -H "Content-Type: application/json" \
  -d '{"user_id":"cB","prompt":"b"}' > /tmp/c2.json &
CURL2_PID=$!
wait "$CURL1_PID" "$CURL2_PID"
C1=$(python3 -c "import json;print(json.load(open('/tmp/c1.json'))['task_id'])")
C2=$(python3 -c "import json;print(json.load(open('/tmp/c2.json'))['task_id'])")
sleep 5
S1=$(curl -s http://localhost:8000/api/task-status/"$C1")
S2=$(curl -s http://localhost:8000/api/task-status/"$C2")
echo "  task1: $S1"
echo "  task2: $S2"
START1=$(echo "$S1" | python3 -c "import json,sys;print(json.load(sys.stdin)['started_at'])")
START2=$(echo "$S2" | python3 -c "import json,sys;print(json.load(sys.stdin)['started_at'])")
if [ "$START1" = "$START2" ]; then pass "both tasks started at the same timestamp (real overlap)"; else fail "start times differ ($START1 vs $START2) — check for accidental serialization"; fi

echo
echo "=== 7. Task-failure check: kill executor mid-flight, submit again ==="
if [ -n "${MOCK_PID:-}" ]; then
  kill -9 "$MOCK_PID" 2>/dev/null
  RESP=$(curl -s -X POST http://localhost:8000/api/submit-task -H "Content-Type: application/json" \
    -d '{"user_id":"u3","prompt":"should fail"}')
  TID3=$(echo "$RESP" | python3 -c "import json,sys;print(json.load(sys.stdin)['task_id'])")
  sleep 2
  FSTATUS=$(curl -s -o /tmp/f3.json -w "%{http_code}" http://localhost:8000/api/task-status/"$TID3")
  BODY3=$(cat /tmp/f3.json)
  echo "  http=$FSTATUS body=$BODY3"
  if [ "$FSTATUS" = "200" ] && echo "$BODY3" | grep -q '"status":"failed"' && echo "$BODY3" | grep -q '"error"'; then
    pass "executor-down task -> 200 OK, status:failed, populated error (not a crash)"
  else
    fail "executor-down handling wrong"
  fi
  # restart mock for cleanliness in case script re-run manually
else
  echo "  skipped (SKIP_MOCK=1, can't kill a real executor from this script)"
fi

echo
echo "======================================"
echo "RESULTS: $PASS passed, $FAIL failed"
echo "======================================"
[ "$FAIL" -eq 0 ] && exit 0 || exit 1
