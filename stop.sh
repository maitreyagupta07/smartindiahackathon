#!/usr/bin/env bash
# Stop the local services started by run.sh.
#
# Must actually stop them. The previous version called `kill $pids` on PIDs
# harvested from Windows `netstat`; Git-Bash's `kill` does not understand a
# native Windows PID, returns success anyway, and so the `|| taskkill`
# fallback never ran. The server survived, `run.sh` then started nothing,
# and the box kept serving stale code while looking restarted — which is
# exactly how a deployed auth fix appeared to "not work".
cd "$(dirname "$0")"

# Detect Git-Bash/MSYS/Cygwin: there, taskkill is the only thing that works.
case "$(uname -s 2>/dev/null)" in
  MINGW*|MSYS*|CYGWIN*) IS_WINDOWS=1 ;;
  *) IS_WINDOWS=0 ;;
esac

stopped_any=0
for port in 8000 8001 8002; do
  if [ "$IS_WINDOWS" = "0" ] && command -v lsof >/dev/null 2>&1; then
    pids="$(lsof -ti "tcp:$port" || true)"
  else
    # Line shape: "  TCP    0.0.0.0:8000   ...   LISTENING   <pid>"
    pids="$(netstat -ano 2>/dev/null | grep -i "LISTENING" | grep ":$port " | awk '{print $NF}' | sort -u)"
  fi

  [ -n "$pids" ] || continue
  echo ">> Stopping service on :$port (pid $pids)"
  for p in $pids; do
    if [ "$IS_WINDOWS" = "1" ]; then
      taskkill //F //PID "$p" >/dev/null 2>&1 || taskkill /F /PID "$p" >/dev/null 2>&1 || true
    else
      kill "$p" 2>/dev/null || true
    fi
  done
  stopped_any=1
done

# Verify, rather than assume. A port still listening here is the failure
# mode this script exists to prevent, so say so loudly instead of "Done."
sleep 1
still=""
for port in 8000 8001 8002; do
  if command -v lsof >/dev/null 2>&1 && [ "$IS_WINDOWS" = "0" ]; then
    lsof -ti "tcp:$port" >/dev/null 2>&1 && still="$still $port"
  else
    netstat -ano 2>/dev/null | grep -i "LISTENING" | grep -q ":$port " && still="$still $port"
  fi
done

rm -f .run-logs/*.pid 2>/dev/null || true

if [ -n "$still" ]; then
  echo ">> [!!] STILL LISTENING on:$still — the old server is alive."
  echo ">>      Do NOT run ./run.sh yet; it would fail to bind the port."
  echo ">>      Windows: netstat -ano | findstr :8000   then  taskkill /F /PID <pid>"
  exit 1
fi

[ "$stopped_any" = "1" ] || echo ">> Nothing was running."
echo ">> Done."
