#!/usr/bin/env bash
# Compile main + test sources with the JDK's javac (no external dependencies).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$ROOT/out/classes"

rm -rf "$OUT"
mkdir -p "$OUT"

echo "[build] javac -> $OUT"
find "$ROOT/src/main/java" "$ROOT/src/test/java" -name '*.java' | sort > "$OUT/sources.list"
javac -d "$OUT" @"$OUT/sources.list"

echo "[build] done"
