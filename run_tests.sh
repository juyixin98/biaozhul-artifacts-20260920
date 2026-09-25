#!/usr/bin/env bash
# Run the full automated test suite (stdlib unittest, no extra deps).
set -euo pipefail
cd "$(dirname "$0")"
export PYTHONPATH="$(pwd)/src${PYTHONPATH:+:$PYTHONPATH}"
python3 -m unittest discover -s tests -v
