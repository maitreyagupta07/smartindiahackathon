"""
Single shared config loader for the merged single-node Workbench app.

Before the single-node refactor, four independent processes (backend,
agent, tools, and each test file) each read the same root config.json with
their own ad-hoc loader and their own FILES_DIR-resolution logic — a
fragile pattern that already caused one real "two processes resolve the
same relative path to two different physical directories" bug. Collapsing
to one process removes that whole class of bug: there is now exactly one
place that reads config.json and resolves FILES_DIR, and every module in
this app imports it from here.

config.json's shape is UNCHANGED by this refactor — this is an internal
consumption detail, not a contract the browser or any external caller sees.
"ports.tools" and "ports.agent_executor" are no longer used to bind
anything (those services no longer exist as separate network listeners),
but are left in config.json/read here for backward compatibility with any
external tooling that inspects the file — nothing breaks if they're absent.
"""
import json
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", REPO_ROOT / "config.json"))


def _load_raw() -> dict:
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


_RAW = _load_raw()
_PORTS = _RAW.get("ports", {}) if isinstance(_RAW.get("ports"), dict) else {}

# The single LAN-facing port — the only network listener this app binds.
BACKEND_PORT = int(_PORTS.get("backend", 8000))

# Ollama stays a separate local process (a third-party binary, not our
# code) — bound to localhost only, invoked over HTTP internally, never
# exposed to the LAN. Never used to bind anything in this app.
INFERENCE_HOST = _RAW.get("inference_host", "localhost")
INFERENCE_PORT = int(_PORTS.get("inference", 11434))

MODELS = _RAW.get("models", {})

# One physical shared-files directory for the whole app — generated files
# are written here (app/tools/filegen.py) and served from here (app/api's
# /files/ mount). Anchored to REPO_ROOT, not the process's cwd, so it
# resolves the same way regardless of which directory `run.sh` is launched
# from.
_raw_files_dir = Path(_RAW.get("FILES_DIR", "./shared_files"))
FILES_DIR = _raw_files_dir if _raw_files_dir.is_absolute() else (REPO_ROOT / _raw_files_dir).resolve()
FILES_DIR.mkdir(parents=True, exist_ok=True)

# Original bytes of every file added to the persistent, per-operator global
# Knowledge Base (app/api/knowledge.py). Kept OUTSIDE FILES_DIR on purpose:
# FILES_DIR is scanned by docsearch's corpus auto-sync and served wholesale
# at /files/, neither of which should apply to a user's private KB uploads.
# Files here are only ever served back through the authenticated-by-user_id
# /api/kb/{document_id}/raw endpoint. Layout: kb_store/<user_id>/<document_id>/<filename>.
_raw_kb_store_dir = Path(_RAW.get("KB_STORE_DIR", "./kb_store"))
KB_STORE_DIR = _raw_kb_store_dir if _raw_kb_store_dir.is_absolute() else (REPO_ROOT / _raw_kb_store_dir).resolve()
KB_STORE_DIR.mkdir(parents=True, exist_ok=True)

MAX_CONCURRENT_TASKS = int(_RAW.get("backend", {}).get("max_concurrent_tasks", 2))

# Vision/heavy tasks get their own, smaller concurrency slice than plain
# text/document tasks — see the concurrency-safety section of app/main.py.
# Not in config.json (nothing external needs to tune this yet); a sane
# fixed default so a mid-range single GPU never gets more than one
# simultaneous vision inference.
MAX_CONCURRENT_VISION_TASKS = int(_RAW.get("backend", {}).get("max_concurrent_vision_tasks", 1))
