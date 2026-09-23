#!/usr/bin/env bash
# Builds the project with plain javac (no Maven/Gradle, no external dependencies).
set -euo pipefail
cd "$(dirname "$0")"

OUT=build/classes
rm -rf "$OUT"
mkdir -p "$OUT"
find src -name '*.java' > build/sources.txt
javac -d "$OUT" @build/sources.txt
echo "compiled main sources -> $OUT"
