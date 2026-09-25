#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p build/main build/test
javac -encoding UTF-8 -d build/main $(find src/main/java -name '*.java')
javac -encoding UTF-8 -cp build/main -d build/test $(find src/test/java -name '*.java')
echo "BUILD_OK"
