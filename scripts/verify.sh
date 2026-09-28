#!/usr/bin/env bash
# Full local verification: static compile, pytest suite, and the independent
# fixture harness.  Fully offline; no accounts or external services.
set -euo pipefail

cd "$(dirname "$0")/.."

echo "== 1/3 byte-compile =="
python3 -m compileall -q htn_planner scripts

echo "== 2/3 pytest suite =="
python3 -m pytest tests "$@"

echo "== 3/3 independent fixture verification =="
python3 scripts/verify.py
