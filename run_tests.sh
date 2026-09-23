#!/usr/bin/env bash
# Run every automated test in the project.
# Usage: bash run_tests.sh
set -e
cd "$(dirname "$0")"
python3 -m unittest discover -s tests -v "$@"
