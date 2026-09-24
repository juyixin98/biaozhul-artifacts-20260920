#!/usr/bin/env bash
# Runs every test: unit/integration (Postgres tests activate when
# ROLLOUT_TEST_DATABASE_URL is set) plus the end-to-end acceptance program.
# Usage: scripts/test.sh [server base url]
set -euo pipefail
cd "$(dirname "$0")/.."

BASE_URL="${1:-http://localhost:8080}"

echo "== go vet =="
go vet ./...

echo "== unit tests (no database required) =="
go test ./...

echo
echo "== end-to-end acceptance =="
go run ./cmd/acceptance -url "$BASE_URL"
