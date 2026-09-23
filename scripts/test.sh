#!/usr/bin/env bash
# Run the automated test suite (engine tests + real HTTP end-to-end test).
set -euo pipefail
cd "$(dirname "$0")/.."

JAVA_BIN="${JAVA_HOME:+$JAVA_HOME/bin/}java"

if [ ! -d out/classes ] || [ ! -d out/test-classes ]; then
  ./scripts/build.sh
fi

"$JAVA_BIN" -cp out/classes:out/test-classes com.example.sessionwindow.Tests
