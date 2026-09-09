#!/usr/bin/env bash
# Single-node Workbench startup.
#
# Starts (or confirms) the local Ollama process, then the ONE app — the
# only thing that binds a LAN-facing port. Docker is a required system
# dependency for the code-execution sandbox (app/tools/sandbox.py) — this
# script does not start Docker itself, only checks it's reachable.
#
# Usage:
#   ./run.sh
# Then open:
#   http://<this-machine's-LAN-IP>:8000/
set -euo pipefail
cd "$(dirname "$0")"

# Windows only ships `python` (no `python3` shim); prefer python3 where it
# exists (native Linux/macOS) so behavior there is unchanged.
PY=python3
command -v python3 >/dev/null 2>&1 || PY=python

# Windows venvs lay out Scripts/ instead of bin/ — everything below reads
# through this instead of hardcoding one or the other.
VENV_BIN=".venv/bin"
[ -d "$VENV_BIN" ] || VENV_BIN=".venv/Scripts"

PORT="$("$PY" -c "import json; print(json.load(open('config.json'))['ports']['backend'])" 2>/dev/null || echo 8000)"

echo "=== Sovereign On-Premise Agentic AI Workbench ==="

# --- Ollama: separate local process, bound to localhost only ---
if curl -s -o /dev/null --max-time 2 http://127.0.0.1:11434/api/tags; then
    echo "[ok] Ollama already running on localhost:11434"
else
    echo "[..] Starting Ollama (localhost-only)..."
    OLLAMA_HOST=127.0.0.1:11434 nohup ollama serve > /tmp/ollama.log 2>&1 &
    sleep 2
    if ! curl -s -o /dev/null --max-time 3 http://127.0.0.1:11434/api/tags; then
        echo "[!!] Ollama did not come up — check /tmp/ollama.log"
        exit 1
    fi
    echo "[ok] Ollama started"
fi

# --- Docker: required for the code-execution sandbox ---
if docker info > /dev/null 2>&1; then
    echo "[ok] Docker reachable (code-execution sandbox available)"
else
    echo "[!!] Docker is not reachable — code-execution tasks will fail."
    echo "     Install/start Docker, or code-execution requests will error out cleanly."
fi

# --- Python deps ---
if [ ! -d ".venv" ]; then
    echo "[..] Creating .venv and installing dependencies (first run only)..."
    "$PY" -m venv .venv
    "./$VENV_BIN/pip" install -q -r requirements.txt
fi

# hostname -I is Linux-only; Windows has no equivalent one-liner, so fall
# back to a Python/socket lookup that works everywhere.
LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}')" || LAN_IP=""
if [ -z "$LAN_IP" ]; then
    LAN_IP="$("$PY" -c "import socket; s=socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(('8.8.8.8', 80)); print(s.getsockname()[0])" 2>/dev/null)" || LAN_IP="<this-machine-ip>"
fi

echo "[..] Starting the Workbench API on 0.0.0.0:${PORT}..."
echo
echo "    Local:  http://localhost:${PORT}/"
echo "    LAN:    http://${LAN_IP}:${PORT}/"
echo
exec "./$VENV_BIN/uvicorn" app.main:app --host 0.0.0.0 --port "${PORT}"
