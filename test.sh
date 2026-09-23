#!/usr/bin/env bash
# Build (if needed) and run the automated test suite.
set -euo pipefail
cd "$(dirname "$0")"

./build.sh

echo "== running tests =="
java -cp build/classes:build/test-classes com.example.uninorm.TestRunner
