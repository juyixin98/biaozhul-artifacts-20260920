#!/usr/bin/env bash
# Build (if needed) and run the automated test suite. Exit code reflects test results.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d out/classes ] || [ ! -d out/test-classes ]; then
  ./build.sh
fi

java -cp out/classes:out/test-classes com.example.topk.TestRunner
