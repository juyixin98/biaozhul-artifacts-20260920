#!/usr/bin/env bash
# Builds the project and runs the full test suite:
#   1. C++ unit + randomized tests (Hopcroft-Karp vs naive exhaustive reference)
#   2. Python end-to-end tests against the CLI (brute-force verification)
#   3. Example requests from examples/
set -u
cd "$(dirname "$0")/.."

fail=0

echo "== build =="
make all || exit 1

echo "== C++ unit/randomized tests =="
for seed in 1 7 12345 999983; do
  ./build/run_tests "$seed" || fail=1
done

echo "== Python end-to-end tests =="
python3 tests/e2e.py ./build/bmatch 300 20260925 || fail=1

echo "== example requests =="
for f in examples/request_basic.json examples/request_isolated_duplicates.json examples/request_numeric_ids.json; do
  echo "--- $f"
  ./build/bmatch "$f" || fail=1
done

echo "--- examples/request_bad_unknown_vertex.json (expect ok:false, exit 1)"
out=$(./build/bmatch examples/request_bad_unknown_vertex.json)
code=$?
echo "$out"
if [ "$code" -ne 1 ]; then
  echo "FAIL: expected exit code 1, got $code"
  fail=1
fi

if [ "$fail" -eq 0 ]; then
  echo "ALL TESTS PASSED"
else
  echo "SOME TESTS FAILED"
fi
exit "$fail"
