#!/usr/bin/env bash
# Run the whole automated test suite (standard library only, no pip install).
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="$(pwd):${PYTHONPATH:-}"
exec python3 -m unittest discover -s tests -p 'test_*.py' -v
