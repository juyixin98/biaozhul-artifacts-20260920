#!/usr/bin/env bash
# Compiles and runs all automated tests. Exits non-zero on any failure.
set -euo pipefail
cd "$(dirname "$0")"

./build.sh

TEST_OUT=build/test
rm -rf "$TEST_OUT"
mkdir -p "$TEST_OUT"
find test -name '*.java' > build/test-sources.txt
javac -cp build/classes -d "$TEST_OUT" @build/test-sources.txt
java -cp build/classes:"$TEST_OUT" com.example.sessionwindow.tests.AllTests
