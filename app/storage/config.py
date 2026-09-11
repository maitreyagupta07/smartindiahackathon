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

# Person E's application-level network monitor (app/monitor/network.py).
# Everything here is an allow-policy input, not a binding target — the
# monitor only ever READS this machine's own socket table. Obsolete
# service ports (8001/8002) are deliberately absent: those processes no
# longer exist in the single-node build.
_NM = _RAW.get("network_monitor", {}) if isinstance(_RAW.get("network_monitor"), dict) else {}
NETWORK_MONITOR = {
    "app_port": int(_NM.get("app_port", BACKEND_PORT)),
    "ollama_port": int(_NM.get("ollama_port", INFERENCE_PORT)),
    # Known frontend/client machine(s) on the venue LAN — traffic to/from
    # these is expected (LAN_CLIENT), never a violation.
    "expected_client_ips": [str(ip) for ip in _NM.get("expected_client_ips", []) if ip],
    # This machine's own LAN address (the ":8000" the client connects to).
    "expected_lan_server_ip": _NM.get("expected_lan_server_ip") or None,
}

# In-process egress firewall (app/security/egress_firewall.py) — the
# ENFORCEMENT counterpart to the read-only monitor above. Installed at app
# startup; blocks any outbound connection from this process to a publicly
# routable address. Loopback + RFC1918 + link-local + the LAN peers below
# are always allowed; anything else here widens the allow-list.
_EF = _RAW.get("egress_firewall", {}) if isinstance(_RAW.get("egress_firewall"), dict) else {}
_ef_self_test = []
for _pair in _EF.get("self_test_targets", []) or []:
    try:
        _host, _port = _pair[0], int(_pair[1])
        if _host:
            _ef_self_test.append((str(_host), _port))
    except (TypeError, ValueError, IndexError):
        continue
EGRESS_FIREWALL = {
    "enabled": bool(_EF.get("enabled", True)),
    # Operator-added always-allowed networks / hosts, on top of the built-in
    # loopback + private-range policy. Plus the monitor's known LAN peers so
    # the two layers can never disagree about what "the LAN" is.
    "extra_allowed_cidrs": [str(c) for c in _EF.get("extra_allowed_cidrs", []) if c],
    "extra_allowed_ips": (
        [str(i) for i in _EF.get("extra_allowed_ips", []) if i]
        + list(NETWORK_MONITOR["expected_client_ips"])
        + ([NETWORK_MONITOR["expected_lan_server_ip"]] if NETWORK_MONITOR["expected_lan_server_ip"] else [])
    ),
    # Public endpoints the /api/egress-firewall/self-test probe tries to
    # reach (and must fail to reach). host, port pairs.
    "self_test_targets": _ef_self_test,
}

# Operators who hold the Admin role (the audit log and network-status
# endpoints). Listed here rather than hardcoded so who counts as staff is a
# deployment decision, editable without touching code — and so an install
# whose first account was a demo/test user can still designate an admin.
# The first account on a fresh database is also made admin automatically
# (see app/api/auth.py), which covers a clean deployment with no config.
ADMIN_USER_IDS = frozenset(
    str(u).strip() for u in (_RAW.get("admin_user_ids") or []) if str(u).strip()
)

# Shared demo-grade passcode for the Admin shell's "Switch to Admin" gate
# (app/api/auth.py's /api/auth/admin-login, frontend/admin-login.html).
# Entering it promotes the caller to is_admin — deliberately a single shared
# secret, not per-account security: this is a single-operator, air-gapped,
# on-premise deployment, so the passcode only exists to keep the Admin
# console from being one click away from the User workbench, not to gate
# between untrusted parties. Override via config.json's "admin_passcode".
ADMIN_PASSCODE = str(_RAW.get("admin_passcode") or "1230#")
