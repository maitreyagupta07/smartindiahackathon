#!/usr/bin/env bash
# Stop the three local services started by run.sh.
cd "$(dirname "$0")"
for port in 8000 8001 8002; do
  pids="$(lsof -ti "tcp:$port" || true)"
  if [ -n "$pids" ]; then
    echo ">> Stopping service on :$port (pid $pids)"
    kill $pids 2>/dev/null || true
  fi
done
rm -f .run-logs/*.pid
echo ">> Done."
