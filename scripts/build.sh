#!/usr/bin/env bash
# Compile all sources with plain javac. Zero third-party dependencies.
set -euo pipefail
cd "$(dirname "$0")/.."

# Pick a JDK: $JAVA_HOME, a local ~/tools install, or javac on PATH.
if [[ -n "${JAVA_HOME:-}" && -x "$JAVA_HOME/bin/javac" ]]; then
  JAVAC="$JAVA_HOME/bin/javac"
  JAVA_BIN="$JAVA_HOME/bin/java"
elif [[ -x "$HOME/tools/jdk-21.0.5+11/bin/javac" ]]; then
  JAVAC="$HOME/tools/jdk-21.0.5+11/bin/javac"
  JAVA_BIN="$HOME/tools/jdk-21.0.5+11/bin/java"
else
  JAVAC="javac"
  JAVA_BIN="java"
fi

echo "Using: $("$JAVAC" -version 2>&1)"
rm -rf out
mkdir -p out/classes out/test out/eval

find src/main/java -name '*.java' > out/main-sources.txt
"$JAVAC" -d out/classes @out/main-sources.txt

find src/test/java -name '*.java' > out/test-sources.txt
"$JAVAC" -cp out/classes -d out/test @out/test-sources.txt

find src/eval/java -name '*.java' > out/eval-sources.txt
"$JAVAC" -cp out/classes -d out/eval @out/eval-sources.txt

echo "Build OK -> out/ (classes, test, eval)"
