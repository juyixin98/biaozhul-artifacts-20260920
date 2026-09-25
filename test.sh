#!/usr/bin/env bash
# Compile (if needed) and run every *Test suite. Exits non-zero on any failure.
set -euo pipefail
cd "$(dirname "$0")"
./build.sh
mkdir -p build/test-classes
find tests -name '*.java' > build/test-sources.txt
javac -d build/test-classes -cp build/classes @build/test-sources.txt

SUITES=(
  approxheavy.tests.JsonTest
  approxheavy.tests.CountMinSketchTest
  approxheavy.tests.BoundedCandidatesTest
  approxheavy.tests.WindowingTest
  approxheavy.tests.ExactReferenceTest
  approxheavy.tests.HttpServerTest
)

FAIL=0
for suite in "${SUITES[@]}"; do
  echo
  if ! java -cp build/classes:build/test-classes "$suite"; then
    FAIL=1
  fi
done

echo
if [ "$FAIL" -eq 0 ]; then
  echo "ALL TEST SUITES PASSED"
else
  echo "TEST FAILURES PRESENT (see above)"
  exit 1
fi
