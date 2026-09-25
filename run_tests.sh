#!/usr/bin/env bash
# Run the full automated test suite.
set -euo pipefail
cd "$(dirname "$0")"
python3 -m unittest discover -s tests -v
