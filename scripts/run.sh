#!/usr/bin/env bash
# One-shot CLI run over the built-in synthetic corpus; prints a JSON report.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
THRESHOLD="${1:-0.6}"

bash "$ROOT/scripts/build.sh" >/dev/null
java -cp "$ROOT/out/classes" neardup.Main run "$THRESHOLD"
