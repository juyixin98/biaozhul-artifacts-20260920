#!/usr/bin/env bash
# Compiles main sources and tests with the JDK's javac. Zero external deps.
set -euo pipefail
cd "$(dirname "$0")"

rm -rf out
mkdir -p out/main out/test

# Allow overriding the JDK, e.g. JAVA_HOME=/path/to/jdk-17 ./build.sh
JAVA_HOME="${JAVA_HOME:-}"
if [[ -n "$JAVA_HOME" ]]; then
  JAVAC="$JAVA_HOME/bin/javac"
else
  JAVAC="javac"
fi

echo ">> Compiling main sources"
find src -name '*.java' > build-main.txt
"$JAVAC" -d out/main @build-main.txt

echo ">> Compiling tests"
find test -name '*.java' > build-test.txt
"$JAVAC" -cp out/main -d out/test @build-test.txt

echo ">> Build OK -> out/main, out/test"
