#!/usr/bin/env bash
# Builds (if needed) and runs the full automated test suite.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -d target/classes ] || [ ! -d target/test-classes ]; then
  scripts/build.sh
fi

java -cp target/classes:target/test-classes streamagg.test.TestAll "$@"
