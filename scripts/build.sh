#!/usr/bin/env bash
# Compiles every main and test source into ./out using only the JDK.
set -euo pipefail
cd "$(dirname "$0")/.."

if ! command -v javac >/dev/null 2>&1; then
  echo "ERROR: javac not found. Install a JDK (built/tested with 17)." >&2
  exit 1
fi

JAVA_MAJOR=$(javac -version 2>&1 | sed -E 's/.* ([0-9]+).*/\1/')
if [ "$JAVA_MAJOR" -lt 17 ]; then
  echo "ERROR: JDK 17+ required, found $(javac -version 2>&1)." >&2
  exit 1
fi

rm -rf out
mkdir -p out
find src -name '*.java' > build-sources.txt
javac -d out @build-sources.txt
if [ -d test ]; then
  find test -name '*.java' > build-test-sources.txt
  javac -cp out -d out @build-test-sources.txt
fi
rm -f build-sources.txt build-test-sources.txt
echo "Build complete: out/"
