#!/usr/bin/env bash
# Run the automated test suite (core unit tests + real HTTP end-to-end).
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ -n "${JAVA_HOME:-}" && -x "$JAVA_HOME/bin/java" ]]; then
  JAVA_BIN="$JAVA_HOME/bin/java"
elif [[ -x "$HOME/tools/jdk-21.0.5+11/bin/java" ]]; then
  JAVA_BIN="$HOME/tools/jdk-21.0.5+11/bin/java"
else
  JAVA_BIN="java"
fi

if [[ ! -d out/classes || ! -d out/test ]]; then
  scripts/build.sh
fi

"$JAVA_BIN" -cp out/classes:out/test com.example.vecsearch.TestRunner
