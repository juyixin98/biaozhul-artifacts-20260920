#!/usr/bin/env bash
# Build (if needed) and run the automated test suite.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d build/classes ] || [ ! -d build/test-classes ]; then
  ./build.sh
fi

java -cp build/classes:build/test-classes partjoin.test.AllTests
