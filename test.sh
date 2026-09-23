#!/usr/bin/env bash
# Builds (if needed) and runs the test suites. Exits non-zero on any failure.
set -euo pipefail
cd "$(dirname "$0")"

JAVA_HOME="${JAVA_HOME:-}"
if [[ -n "$JAVA_HOME" ]]; then
  JAVA="$JAVA_HOME/bin/java"
else
  JAVA="java"
fi

if [[ ! -d out/main || ! -d out/test ]]; then
  ./build.sh
fi

echo ">> Running tests"
"$JAVA" -cp out/main:out/test com.example.iview.TestAll
