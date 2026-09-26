#!/usr/bin/env bash
# Zero-dependency build: compiles all main + test sources with the JDK's javac.
set -euo pipefail
cd "$(dirname "$0")/.."

OUT=build/classes
rm -rf "$OUT"
mkdir -p "$OUT"

echo "[build] compiling main sources"
find src/main/java -name '*.java' > build/main-sources.txt
javac -d "$OUT" @build/main-sources.txt

if [ "${SKIP_TESTS:-0}" != "1" ]; then
  echo "[build] compiling test sources"
  find src/test/java -name '*.java' > build/test-sources.txt
  javac -cp "$OUT" -d "$OUT" @build/test-sources.txt
fi

echo "[build] OK -> $OUT"
