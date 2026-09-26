#!/usr/bin/env bash
# Full verification: clean build -> unit/differential tests -> live API tests.
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "################ 1/3 clean build ################"
make clean
make
BUILD_RC=$?
if [ "$BUILD_RC" -ne 0 ]; then echo "BUILD FAILED ($BUILD_RC)"; exit 1; fi

echo
echo "################ 2/3 unit + differential tests ################"
./build/test_dom
UNIT_RC=$?

echo
echo "################ 3/3 HTTP API tests (real server + curl) ################"
./tests/api_test.sh
API_RC=$?

echo
echo "================ SUMMARY ================"
echo "unit/differential tests : $([ $UNIT_RC -eq 0 ] && echo PASS || echo FAIL)"
echo "HTTP API tests          : $([ $API_RC -eq 0 ] && echo PASS || echo FAIL)"
[ "$UNIT_RC" -eq 0 ] && [ "$API_RC" -eq 0 ]
