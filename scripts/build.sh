#!/usr/bin/env bash
# Reproducible build. No network access beyond the (already committed)
# embedded zone database; GOTOOLCHAIN=local forbids toolchain auto-download.
set -euo pipefail
export PATH="${PATH}:/usr/local/go/bin"
export GOTOOLCHAIN=local
export CGO_ENABLED=0

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

VERSION="${VERSION:-dev}"

mkdir -p bin
go build -trimpath -ldflags="-s -w" -o bin/tztrig ./cmd/tztrig
echo "built bin/tztrig (version tag: ${VERSION})"
