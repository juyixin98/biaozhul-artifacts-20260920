#!/usr/bin/env bash
# Builds (unless already built) and runs the full automated test suite with the built-in runner.
set -euo pipefail
cd "$(dirname "$0")/.."

OUT=build/classes
if [ ! -d "$OUT" ] || [ "${REBUILD:-1}" = "1" ]; then
  scripts/build.sh
fi

TEST_CLASSES=$(find src/test/java -name '*Test.java' -o -name '*IT.java' \
  | sed 's#src/test/java/##; s#/#.#g; s#\.java$##')

echo "[test] running:"
echo "$TEST_CLASSES" | sed 's/^/  - /'
echo
java -cp "$OUT" hlc.TestRunner $TEST_CLASSES
