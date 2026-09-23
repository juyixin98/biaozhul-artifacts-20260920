#!/usr/bin/env bash
# Compile and run all automated tests (no external test framework required).
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p build
rm -rf build/classes build/test-classes
mkdir -p build/classes build/test-classes
find src -name '*.java' > build/sources.txt
find test -name '*.java' > build/test-sources.txt
javac -d build/classes @build/sources.txt
javac -cp build/classes -d build/test-classes @build/test-sources.txt
java -cp build/classes:build/test-classes test.TestMain