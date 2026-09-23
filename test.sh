#!/usr/bin/env bash
# Compiles and runs the zero-dependency end-to-end test suite.
set -euo pipefail
cd "$(dirname "$0")"

if [ -n "${JAVA_HOME:-}" ]; then JAVAC="$JAVA_HOME/bin/javac"; JAVA="$JAVA_HOME/bin/java"; else JAVAC="$(command -v javac)"; JAVA="$(command -v java)"; fi
if [ -z "${JAVA:-}" ] || ! "$JAVA" -version >/dev/null 2>&1; then
  echo "error: java not found. Install JDK 17+ and set JAVA_HOME (see README.md)." >&2
  exit 1
fi

./build.sh
# shellcheck disable=SC2046
"$JAVAC" -Xlint:all -cp build -d build $(find test -name '*.java')
echo "running tests..."
"$JAVA" -cp build com.example.stablepager.StablePagerTests
