#!/usr/bin/env bash
# Compile main sources into target/classes using only the JDK.
set -euo pipefail
cd "$(dirname "$0")/.."

JAVAC="${JAVAC:-javac}"
OUT=target/classes
rm -rf "$OUT"
mkdir -p "$OUT"
find src/main/java -name '*.java' > target/sources.txt
"$JAVAC" -encoding UTF-8 -Xlint:all -d "$OUT" @target/sources.txt
echo "compiled main classes -> $OUT"
