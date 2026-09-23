#!/usr/bin/env bash
# Build the service and tests with the JDK only — no dependency downloads.
set -euo pipefail
cd "$(dirname "$0")/.."

# Locate java: $JAVA_HOME > PATH > the user-local JDK this project was developed with.
if [ -z "${JAVA_HOME:-}" ]; then
  if command -v javac >/dev/null 2>&1; then
    JAVA_HOME="$(dirname "$(dirname "$(readlink -f "$(command -v javac)")")")"
  elif [ -x "$HOME/jdk17/usr/lib/jvm/java-17-openjdk-amd64/bin/javac" ]; then
    JAVA_HOME="$HOME/jdk17/usr/lib/jvm/java-17-openjdk-amd64"
  else
    echo "ERROR: JDK 17+ not found. Set JAVA_HOME or install a JDK." >&2
    exit 1
  fi
fi
export JAVA_HOME
echo "Using JAVA_HOME=$JAVA_HOME"
"$JAVA_HOME/bin/java" -version

rm -rf build
mkdir -p build/classes build/test-classes

echo "== Compiling main sources =="
"$JAVA_HOME/bin/javac" -d build/classes $(find src/main/java -name '*.java')

echo "== Compiling tests =="
"$JAVA_HOME/bin/javac" -d build/test-classes -cp build/classes \
  $(find src/test/java -name '*.java')

echo "Build OK -> build/classes, build/test-classes"
