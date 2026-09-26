#!/usr/bin/env bash
# End-to-end protocol tests for ./dagpaths (deterministic cases).
set -euo pipefail
cd "$(dirname "$0")/.."
python3 tests/e2e_test.py
