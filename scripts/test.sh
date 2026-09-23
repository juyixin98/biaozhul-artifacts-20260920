#!/usr/bin/env bash
# Builds (if needed) and runs every test class. Exits non-zero on the first failure.
set -uo pipefail
cd "$(dirname "$0")/.."

if [ ! -d out ] || [ -z "$(find out -name '*.class' -print -quit 2>/dev/null)" ]; then
  ./scripts/build.sh
fi

TESTS=(
  joinplanner.TestJson
  joinplanner.TestEstimatorAndPlanner
  joinplanner.TestEnumerationCrossCheck
  joinplanner.TestEdgeCases
  joinplanner.TestSimulation
  joinplanner.TestHttp
)

fail=0
for t in "${TESTS[@]}"; do
  if ! timeout 120 java -cp out "$t"; then
    echo "FAILED: $t"
    fail=1
  fi
done

if [ "$fail" -eq 0 ]; then
  echo "All test classes passed."
fi
exit "$fail"
