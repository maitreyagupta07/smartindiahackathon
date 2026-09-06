#!/usr/bin/env bash
# One-command launcher for the Sovereign AI Workbench.
# Serves the API + the integrated frontend together on http://localhost:8000/
set -euo pipefail

cd "$(dirname "$0")"

# First run: create your own config from the template.
if [ ! -f config.json ]; then
  cp config.json.example config.json
  echo ">> Created config.json from config.json.example"
  echo ">> Edit 'inference_host' in config.json if your inference server is not on this machine."
fi

# Use a local venv so every laptop is identical.
if [ ! -d venv ]; then
  python3 -m venv venv
fi
# shellcheck disable=SC1091
source venv/bin/activate

pip install --quiet --upgrade pip
pip install --quiet -r backend/requirements.txt

echo ">> Starting backend + frontend on http://localhost:8000/"
cd backend
exec python main.py
