#!/usr/bin/env bash
# One-command launcher for the Sovereign AI Workbench.
#
# Starts all three local services so the integrated frontend actually works
# (no "connection failed" / "executor unreachable"):
#
#   tools service    -> http://localhost:8001   (Person C)
#   agent executor   -> http://localhost:8002   (Person F)
#   backend + frontend -> http://localhost:8000 (Person B)  <-- open this
#
# Inference (Ollama) must be reachable at  inference_host:11434  from config.json.
set -euo pipefail
cd "$(dirname "$0")"
ROOT="$(pwd)"
LOGDIR="$ROOT/.run-logs"
mkdir -p "$LOGDIR"

# --- config -----------------------------------------------------------------
if [ ! -f config.json ]; then
  cp config.json.example config.json
  echo ">> Created config.json from config.json.example"
fi
INFER_HOST="$(python3 -c 'import json;print(json.load(open("config.json"))["inference_host"])')"
INFER_PORT="$(python3 -c 'import json;print(json.load(open("config.json"))["ports"]["inference"])')"

# --- venv + deps ----------------------------------------------------------
if [ ! -d venv ]; then python3 -m venv venv; fi
# shellcheck disable=SC1091
source venv/bin/activate
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r backend/requirements.txt
python -m pip install --quiet -r agent/requirements.txt
python -m pip install --quiet -r tools/requirements.txt

# --- inference reachability check ---------------------------------------
echo ">> Checking inference at http://$INFER_HOST:$INFER_PORT ..."
if ! curl -sf -m 5 "http://$INFER_HOST:$INFER_PORT/api/tags" >/dev/null; then
  echo "!! Inference server not reachable at $INFER_HOST:$INFER_PORT."
  echo "!! Start Ollama locally ('ollama serve') and set inference_host to 127.0.0.1 in config.json,"
  echo "!! or point inference_host at a machine on your LAN that is running it."
  exit 1
fi
echo ">> Inference OK"

# --- free the ports, then start each service --------------------------
start() {  # name  port  workdir  cmd...
  local name=$1 port=$2 wd=$3; shift 3
  local pids; pids="$(lsof -ti "tcp:$port" || true)"
  [ -n "$pids" ] && { echo ">> Freeing port $port"; kill $pids 2>/dev/null || true; sleep 1; }
  echo ">> Starting $name on :$port  (log: .run-logs/$name.log)"
  ( cd "$wd" && nohup "$@" >"$LOGDIR/$name.log" 2>&1 & echo $! >"$LOGDIR/$name.pid" )
}

wait_health() {  # name  url
  local name=$1 url=$2 i
  for i in $(seq 1 40); do
    curl -sf -m 3 "$url" >/dev/null && { echo ">> $name is up"; return 0; }
    sleep 0.5
  done
  echo "!! $name did not become healthy — see .run-logs/$name.log"; return 1
}

start tools  8001 "$ROOT/tools"    python -m app.main
start agent  8002 "$ROOT/agent"    python main.py
start backend 8000 "$ROOT/backend" python main.py

wait_health tools   "http://localhost:8001/health"
wait_health agent   "http://localhost:8002/health"
wait_health backend "http://localhost:8000/api/audit-log"

echo
echo "=============================================================="
echo "  Open the integrated frontend:   http://localhost:8000/"
echo "  Stop everything:                ./stop.sh"
echo "=============================================================="
