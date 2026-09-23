#!/usr/bin/env bash
# Compiles main sources into build/. Requires JDK 17+ (only javac; no external deps).
set -euo pipefail
cd "$(dirname "$0")"

if [ -n "${JAVA_HOME:-}" ]; then JAVAC="$JAVA_HOME/bin/javac"; else JAVAC="$(command -v javac)"; fi
if [ -z "${JAVAC:-}" ] || ! "$JAVAC" -version >/dev/null 2>&1; then
  echo "error: javac not found. Install JDK 17+ and set JAVA_HOME (see README.md)." >&2
  exit 1
fi

rm -rf build
mkdir -p build
# shellcheck disable=SC2046
"$JAVAC" -Xlint:all -d build $(find src -name '*.java')
echo "compiled main sources -> build/"
