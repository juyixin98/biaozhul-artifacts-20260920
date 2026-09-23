#!/usr/bin/env bash
# Run the full automated test suite (unit + gRPC/ SQLite integration) with the
# race detector. Uses vendored dependencies so it works offline.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
go test -mod=vendor -race -count=1 ./...
