#!/usr/bin/env bash
# Run the built-in test suite. Exits non-zero if any test fails.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d target/classes ] || [ ! -d target/test-classes ]; then
  ./build.sh
fi
java -cp target/classes:target/test-classes com.opp16.engine.tests.RunTests
