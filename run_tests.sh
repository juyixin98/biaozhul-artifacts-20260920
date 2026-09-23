#!/usr/bin/env bash
# Convenience: run the unit/integration suite, then the exhaustive
# differential validation over the example programs.
set -e
cd "$(dirname "$0")"
python3 -m unittest discover -s tests -p 'test_*.py' "$@"
python3 scripts/validate.py
