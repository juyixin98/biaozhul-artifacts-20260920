#!/usr/bin/env bash
# Compile main and test sources into build/. Zero external dependencies.
set -euo pipefail
cd "$(dirname "$0")"

rm -rf build
mkdir -p build/main build/test

find src/main/java -name '*.java' | sort > build/main-sources.txt
javac -encoding UTF-8 -d build/main @build/main-sources.txt

find src/test/java -name '*.java' | sort > build/test-sources.txt
javac -encoding UTF-8 -cp build/main -d build/test @build/test-sources.txt

echo "build OK"
