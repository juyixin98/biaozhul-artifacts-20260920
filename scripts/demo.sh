#!/usr/bin/env bash
# Convenience wrapper for the end-to-end acceptance demo.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -d .venv ]; then
  python3 -m venv .venv
  . .venv/bin/activate
  pip install --upgrade pip
  pip install -r requirements.lock
else
  . .venv/bin/activate
fi

exec python scripts/demo.py
