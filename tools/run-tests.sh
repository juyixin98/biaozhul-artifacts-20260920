#!/usr/bin/env bash
# Compile and run the full automated test suite. Exit code 0 only if all pass.
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ -n "${JAVA_HOME:-}" ]]; then
  JAVA="$JAVA_HOME/bin/java"
else
  JAVA="java"
fi

tools/build.sh
exec "$JAVA" -cp classes joinplanner.test.AllTests
