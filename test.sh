#!/usr/bin/env bash
# Build and run the full automated test suite.
set -euo pipefail
cd "$(dirname "$0")"

./build.sh
java -cp build/main:build/test com.timeconv.TestMain "$(pwd)"
