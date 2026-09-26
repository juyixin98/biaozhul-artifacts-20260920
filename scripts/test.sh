#!/usr/bin/env bash
# Runs the full test suite and emits structured (JSON) test results to
# results/test-results.json plus a human-readable summary.
set -euo pipefail
cd "$(dirname "$0")/.."

mkdir -p results
go test -json -count=1 ./... > results/test-results.json || true

# Summarize per-package results from the JSON stream.
go run ./internal/testsum results/test-results.json
