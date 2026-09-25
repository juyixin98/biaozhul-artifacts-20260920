#!/usr/bin/env bash
# Build (if needed) and run the automated test suite.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

bash "$ROOT/scripts/build.sh"

echo
echo "[test] running neardup.AllTests"
java -cp "$ROOT/out/classes" neardup.AllTests
