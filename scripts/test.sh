#!/usr/bin/env bash
# Runs the automated test suite. Exits non-zero if any test fails.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -d build/classes ] || [ ! -d build/test-classes ]; then
    scripts/build.sh
fi

java -cp build/classes:build/test-classes com.example.positiondiff.Tests
