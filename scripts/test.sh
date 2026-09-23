#!/usr/bin/env bash
# Run the full automated test suite.
set -euo pipefail
cd "$(dirname "$0")/.."

./scripts/build.sh
echo "== running tests =="
java -cp build/classes:build/test-classes seqcep.AllTests
