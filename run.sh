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

# Git-Bash/MSYS/Cygwin behave differently enough (no lsof, bash `kill` cannot
# touch native Windows PIDs, venvs use Scripts/ not bin/) that the few places
# it matters branch on this explicitly. The deployment runs on Windows.
case "$(uname -s 2>/dev/null)" in
  MINGW*|MSYS*|CYGWIN*) IS_WINDOWS=1 ;;
  *) IS_WINDOWS=0 ;;
esac

# Windows venvs lay out Scripts/ instead of bin/ — everything below reads
# through this instead of hardcoding one or the other.
VENV_BIN=".venv/bin"
[ -d "$VENV_BIN" ] || VENV_BIN=".venv/Scripts"

PORT="$("$PY" -c "import json; print(json.load(open('config.json'))['ports']['backend'])" 2>/dev/null || echo 8000)"

echo "=== ERSA — On-Premise Agentic AI Workbench ==="

# --- Locate Ollama even if it isn't on PATH in this shell -------------------
# The Windows box uses the portable build under %LOCALAPPDATA%. Git-Bash
# started from Explorer / the IDE often doesn't inherit that on PATH, which
# is why `ollama serve` used to silently do nothing.
if ! command -v ollama >/dev/null 2>&1; then
    for cand in \
        "$LOCALAPPDATA/Programs/OllamaPortable" \
        "$LOCALAPPDATA/Programs/Ollama" \
        "$HOME/AppData/Local/Programs/OllamaPortable" \
        "$HOME/AppData/Local/Programs/Ollama" \
        "/c/Program Files/Ollama"; do
        if [ -x "$cand/ollama" ] || [ -x "$cand/ollama.exe" ]; then
            PATH="$cand:$PATH"
            echo "[ok] Found Ollama at $cand (added to PATH for this run)"
            break
        fi
    done
fi

# --- Ollama: separate local process, bound to localhost only ---
#
# This block must never abort the script. It used to `exit 1` when Ollama
# didn't answer within 2 seconds, which meant uvicorn below was never
# reached — so a slow or manually-managed Ollama silently left the whole
# Workbench down, and (worse) left the PREVIOUS server process running and
# serving stale code. A missing Ollama only breaks inference; the API,
# the UI and sign-in all work without it, and inference errors out cleanly
# on its own. Warn loudly, carry on.
mkdir -p .run-logs
OLLAMA_LOG=".run-logs/ollama.log"

ollama_up() { curl -s -o /dev/null --max-time 2 http://127.0.0.1:11434/api/tags; }

if ollama_up; then
    echo "[ok] Ollama already running on localhost:11434"
else
    echo "[..] Starting Ollama (localhost-only)..."
    # On Windows Ollama is usually a tray app/service already holding the
    # port; if `ollama` isn't on PATH in Git-Bash this just fails and we
    # fall through to the warning rather than killing the startup.
    OLLAMA_HOST=127.0.0.1:11434 nohup ollama serve > "$OLLAMA_LOG" 2>&1 &

    # Cold start on Windows regularly takes longer than the old 2s+3s
    # budget, especially on first run after boot. Poll instead of guessing.
    for _ in $(seq 1 20); do
        ollama_up && break
        sleep 1
    done

    if ollama_up; then
        echo "[ok] Ollama started"
    else
        echo "[!!] WARNING: Ollama is NOT reachable on 127.0.0.1:11434."
        echo "     The Workbench will still start and you can sign in, but any"
        echo "     model request will fail until Ollama is up."
        echo "     Check $OLLAMA_LOG, or start it yourself: ollama serve"
    fi
fi

# --- Ollama models: make sure the ones config.json references are present ---
# A first run on a fresh box otherwise 500s the first real request with
# "model not found". Pulls are no-ops when the model is already local.
if ollama_up && command -v ollama >/dev/null 2>&1; then
    NEED_MODELS="$("$PY" -c "import json,sys; m=json.load(open('config.json'))['models']; print(' '.join(dict.fromkeys([m.get('text_model'),m.get('vision_model')])))" 2>/dev/null || echo "qwen3:1.7b moondream")"
    HAVE="$(ollama list 2>/dev/null || true)"
    for M in $NEED_MODELS; do
        [ -n "$M" ] || continue
        if printf '%s\n' "$HAVE" | grep -q "^${M%%:*}"; then
            echo "[ok] Ollama model present: $M"
        else
            echo "[..] Pulling Ollama model: $M (first run only)..."
            ollama pull "$M" || echo "[!!] Could not pull $M — model requests for it will fail."
        fi
    done
    # The approval-note LoRA is a local Modelfile, not a registry pull.
    LORA_NAME="$("$PY" -c "import json;print(json.load(open('config.json'))['models'].get('lora_adapter') or '')" 2>/dev/null || echo "approval-note-lora")"
    if [ -n "$LORA_NAME" ] && ! printf '%s\n' "$HAVE" | grep -q "^${LORA_NAME}"; then
        if [ -f "inference/lora/Modelfile.approval-note-lora" ]; then
            echo "[..] Registering LoRA model '$LORA_NAME' from inference/lora/Modelfile.approval-note-lora ..."
            ( cd inference/lora && ollama create "$LORA_NAME" -f Modelfile.approval-note-lora ) \
                || echo "[!!] Could not register $LORA_NAME — approval-note generation will fall back to the base text model."
        fi
    fi
fi

# --- Docker: required for the code-execution sandbox -----------------------
docker_up() { docker info > /dev/null 2>&1; }

if docker_up; then
    echo "[ok] Docker reachable (code-execution sandbox available)"
else
    if [ "$IS_WINDOWS" = "1" ]; then
        echo "[..] Docker not reachable — trying to start Docker Desktop..."
        for dd in \
            "/c/Program Files/Docker/Docker/Docker Desktop.exe" \
            "$LOCALAPPDATA/Docker/Docker Desktop.exe"; do
            [ -f "$dd" ] && { "$dd" >/dev/null 2>&1 & break; }
        done
        for _ in $(seq 1 60); do
            docker_up && break
            sleep 2
        done
    fi
    if docker_up; then
        echo "[ok] Docker reachable (code-execution sandbox available)"
    else
        echo "[!!] Docker is NOT reachable — code-execution tasks will error out cleanly."
        echo "     Start Docker Desktop manually, then code-execution will work without restarting ERSA."
    fi
fi

# Pre-pull the sandbox image so the very first code-execution request isn't
# slowed (or failed on a flaky moment) by an on-the-spot image pull.
if docker_up && ! docker image inspect python:3.11-slim >/dev/null 2>&1; then
    echo "[..] Pulling sandbox image python:3.11-slim (first run only)..."
    docker pull python:3.11-slim || echo "[!!] Could not pre-pull python:3.11-slim — first code task may be slow."
fi

# --- Python deps ---
if [ ! -d ".venv" ]; then
    echo "[..] Creating .venv and installing dependencies (first run only)..."
    "$PY" -m venv .venv
    "./$VENV_BIN/pip" install -q -r requirements.txt
fi

# --- GPU / CUDA preflight -------------------------------------------------
# SD-Turbo (image generation) and MOMENT (forecasting) run through PyTorch,
# which only uses the GPU when a CUDA-enabled torch build is installed. A
# CPU-only wheel makes those two models silently ~10-30x slower. Report the
# real state loudly instead of finding out mid-demo.
GPU_REPORT="$("./$VENV_BIN/python" - <<'PY' 2>/dev/null || true
try:
    import torch
    if torch.cuda.is_available():
        print(f"[ok] PyTorch CUDA OK - {torch.cuda.get_device_name(0)} "
              f"(torch {torch.__version__}); SD-Turbo + MOMENT will run on GPU")
    else:
        print(f"[!!] PyTorch is CPU-ONLY (torch {torch.__version__}). SD-Turbo and MOMENT "
              f"will run on CPU (slow). Reinstall a CUDA build, e.g.:\n"
              f"     ./.venv/Scripts/pip install --force-reinstall torch --index-url https://download.pytorch.org/whl/cu121")
except Exception as e:
    print(f"[!!] Could not check PyTorch/CUDA: {e}")
PY
)"
echo "$GPU_REPORT"

# Ollama uses the GPU on its own when one is visible; surface which.
if ollama_up; then
    if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then
        echo "[ok] NVIDIA GPU visible to the host — Ollama will offload qwen3 + moondream to it"
    else
        echo "[!!] nvidia-smi not available — Ollama may be running models on CPU"
    fi
fi

# hostname -I is Linux-only; Windows has no equivalent one-liner, so fall
# back to a Python/socket lookup that works everywhere.
LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}')" || LAN_IP=""
if [ -z "$LAN_IP" ]; then
    LAN_IP="$("$PY" -c "import socket; s=socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(('8.8.8.8', 80)); print(s.getsockname()[0])" 2>/dev/null)" || LAN_IP="<this-machine-ip>"
fi

# --- Refuse to start on top of an already-running server ---
#
# This is the check whose absence cost a deployment: stop.sh silently failed
# to kill the old process, run.sh aborted earlier at the Ollama gate, and the
# box kept serving the PREVIOUS build's Python while the freshly-pulled
# static frontend was served from disk. Everything looked restarted; the new
# API routes simply did not exist, so the browser got 405s. Fail loudly here
# instead of leaving a stale process serving stale code.
port_in_use() {
    if [ "$IS_WINDOWS" = "0" ] && command -v lsof >/dev/null 2>&1; then
        lsof -ti "tcp:${PORT}" >/dev/null 2>&1
    else
        netstat -ano 2>/dev/null | grep -i "LISTENING" | grep -q ":${PORT} "
    fi
}

if port_in_use; then
    echo "[!!] Port ${PORT} is ALREADY IN USE — an old server is still running."
    echo "     It would keep serving the previous build. Stop it first:"
    echo
    echo "         ./stop.sh"
    echo
    echo "     If that does not clear it (Windows):"
    echo "         netstat -ano | findstr :${PORT}"
    echo "         taskkill /F /PID <pid>"
    exit 1
fi

echo "[..] Starting the Workbench API on 0.0.0.0:${PORT}..."
echo
echo "    Local:  http://localhost:${PORT}/"
echo "    LAN:    http://${LAN_IP}:${PORT}/"
echo
exec "./$VENV_BIN/uvicorn" app.main:app --host 0.0.0.0 --port "${PORT}"
