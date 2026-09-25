#!/usr/bin/env bash
# Compiles main + test sources (zero external dependencies) and runs the
# full test suite. Exit code is non-zero if any test fails.
set -euo pipefail
cd "$(dirname "$0")"

MAIN_OUT=build/classes
TEST_OUT=build/test-classes
mkdir -p "$MAIN_OUT" "$TEST_OUT"

echo "[test] compiling main"
find src/main/java -name '*.java' | sort > build/main-sources.txt
javac -d "$MAIN_OUT" @build/main-sources.txt

echo "[test] compiling tests"
find src/test/java -name '*.java' | sort > build/test-sources.txt
javac -cp "$MAIN_OUT" -d "$TEST_OUT" @build/test-sources.txt

echo "[test] running"
java -cp "$MAIN_OUT:$TEST_OUT" com.example.dedup.tests.AllTests
