#!/usr/bin/env bash
# Stop the three local services started by run.sh.
cd "$(dirname "$0")"
for port in 8000 8001 8002; do
  if command -v lsof >/dev/null 2>&1; then
    pids="$(lsof -ti "tcp:$port" || true)"
  else
    # Git-Bash/Windows has no lsof — fall back to netstat, which is always
    # present. Output line shape: "  TCP    0.0.0.0:8000   ...  LISTENING   <pid>"
    pids="$(netstat -ano 2>/dev/null | grep -i "LISTENING" | grep ":$port " | awk '{print $NF}' | sort -u)"
  fi
  if [ -n "$pids" ]; then
    echo ">> Stopping service on :$port (pid $pids)"
    kill $pids 2>/dev/null || (for p in $pids; do taskkill //F //PID "$p" 2>/dev/null; done) || true
  fi
done
rm -f .run-logs/*.pid
echo ">> Done."
