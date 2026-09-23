#!/usr/bin/env bash
# Compile main + test sources with plain javac (no build tool, no third-party deps).
set -euo pipefail
cd "$(dirname "$0")/.."

# Allow overriding the JDK: JAVA_HOME=/path/to/jdk-17 ./scripts/build.sh
JAVA_BIN="${JAVA_HOME:+$JAVA_HOME/bin/}java"
JAVAC_BIN="${JAVA_HOME:+$JAVA_HOME/bin/}javac"

"$JAVAC_BIN" -version
mkdir -p out/classes out/test-classes
"$JAVAC_BIN" -d out/classes $(find src/main -name '*.java')
"$JAVAC_BIN" -cp out/classes -d out/test-classes $(find src/test -name '*.java')
echo "BUILD OK -> out/classes, out/test-classes"
"$JAVA_BIN" -version
