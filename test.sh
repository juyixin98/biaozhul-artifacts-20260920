#!/usr/bin/env bash
# Compile (if needed) and run the full automated test suite.
set -euo pipefail
cd "$(dirname "$0")"
./build.sh
java -cp out phraseindex.AllTests
