#!/usr/bin/env bash
# Compile and run the zero-dependency test suites, then exit non-zero on failure.
set -euo pipefail
cd "$(dirname "$0")/.."

JAVA_HOME_DIR="${JAVA_HOME:-}"
if [ -n "$JAVA_HOME_DIR" ]; then
  JAVAC="$JAVA_HOME_DIR/bin/javac"
  JAVA="$JAVA_HOME_DIR/bin/java"
else
  JAVAC="${JAVAC:-javac}"
  JAVA="${JAVA:-java}"
fi

MAIN_OUT=build/classes
TEST_OUT=build/test-classes
rm -rf "$TEST_OUT"
mkdir -p "$MAIN_OUT" "$TEST_OUT" build

echo ">> Compiling main..."
find src -name '*.java' > build/sources.txt
"$JAVAC" -Xlint:all -d "$MAIN_OUT" @build/sources.txt

echo ">> Compiling tests..."
find test -name '*.java' > build/test-sources.txt
"$JAVAC" -d "$TEST_OUT" -cp "$MAIN_OUT" @build/test-sources.txt

echo ">> Running tests..."
"$JAVA" -cp "$MAIN_OUT:$TEST_OUT" sessionwindow.AllTests
