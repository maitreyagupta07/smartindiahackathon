"""
Shared config loader.

Reads the repo-root `config.json` (see MASTER_BUILD_GUIDE §2.2 / §2.7a).
Person C owns the `tools_port` key (or `PORT` under a `tools` section,
depending on how the team finally shapes the file) and jointly owns
`FILES_DIR` with Person B.

This module never hardcodes a port or path — everything is read from the
shared config file at repo root, with sane local-dev fallbacks only for
running this service in isolation before config.json exists.
"""
import json
import os
from pathlib import Path

# repo root is two levels up from this file: /tools/app/config.py -> /
REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", REPO_ROOT / "config.json"))

_DEFAULTS = {
    "tools_port": 8001,
    "FILES_DIR": str(REPO_ROOT / "shared_files"),
}


def _load_raw() -> dict:
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def get_config() -> dict:
    raw = _load_raw()
    cfg = dict(_DEFAULTS)

    # Accept a couple of reasonable shapes so this doesn't break if the
    # team's config.json nests ports under a "ports" key.
    if "ports" in raw and isinstance(raw["ports"], dict):
        cfg["tools_port"] = raw["ports"].get("tools", raw["ports"].get("C", cfg["tools_port"]))
    if "tools_port" in raw:
        cfg["tools_port"] = raw["tools_port"]

    if "FILES_DIR" in raw:
        cfg["FILES_DIR"] = raw["FILES_DIR"]

    return cfg


CONFIG = get_config()
TOOLS_PORT = int(CONFIG["tools_port"])

# FILES_DIR from config.json is conventionally a repo-root-relative path
# (e.g. "./shared_files"). Anchor it to REPO_ROOT rather than the process's
# cwd — otherwise this service (documented to be launched from within
# /tools) and Person B's backend (launched from within /backend) resolve
# the *same* config value to two different physical directories, silently
# breaking the /files/<name> URLs this service hands back (§2.7a).
_raw_files_dir = Path(CONFIG["FILES_DIR"])
FILES_DIR = _raw_files_dir if _raw_files_dir.is_absolute() else (REPO_ROOT / _raw_files_dir).resolve()
FILES_DIR.mkdir(parents=True, exist_ok=True)
