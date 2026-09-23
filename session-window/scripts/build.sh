#!/usr/bin/env bash
# Build main sources with the JDK compiler only. No third-party dependencies.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -n "${JAVA_HOME:-}" ]; then
  JAVAC="$JAVA_HOME/bin/javac"
else
  JAVAC="${JAVAC:-javac}"
fi
OUT=build/classes
rm -rf "$OUT"
mkdir -p "$OUT" build

echo ">> Compiling with: $($JAVAC -version 2>&1)"
find src -name '*.java' > build/sources.txt
$JAVAC -Xlint:all -d "$OUT" @build/sources.txt
echo ">> Built $OUT"
