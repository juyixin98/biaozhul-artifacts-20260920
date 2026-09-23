#!/usr/bin/env bash
# Compile and run all tests (engine-level + HTTP end-to-end). No test framework needed.
set -euo pipefail
cd "$(dirname "$0")"

JAVA_HOME="${JAVA_HOME:-}"
if [ -n "$JAVA_HOME" ]; then
  JAVAC="$JAVA_HOME/bin/javac"
  JAVA="$JAVA_HOME/bin/java"
else
  JAVAC="javac"
  JAVA="java"
fi

rm -rf target/classes target/test-classes
mkdir -p target/classes target/test-classes

find src/main/java src/test/java -name '*.java' > target/all-sources.txt
"$JAVAC" --release 17 -encoding UTF-8 -d target/classes @target/all-sources.txt

echo "=== Engine tests ==="
"$JAVA" -cp target/classes join.EngineTests
echo
echo "=== HTTP end-to-end tests ==="
"$JAVA" -cp target/classes join.ApiIT
