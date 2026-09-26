#!/usr/bin/env bash
# End-to-end test driver: unit/differential tests + CLI example regression.
set -euo pipefail
cd "$(dirname "$0")/.."

BIN=build/dynconn

echo "== unit + randomized differential tests =="
./build/test_random

echo "== CLI example regression (offline solver) =="
for f in examples/request_*.json; do
  name=$(basename "$f" .json); name=${name#request_}
  exp="examples/expected_${name}.json"
  "$BIN" "$f" > /tmp/dynconn_out.json
  diff -u "$exp" /tmp/dynconn_out.json
  echo "PASS example $name (offline)"
done

echo "== CLI example regression (reference solver, same expected output) =="
for f in examples/request_*.json; do
  name=$(basename "$f" .json); name=${name#request_}
  exp="examples/expected_${name}.json"
  "$BIN" --reference "$f" > /tmp/dynconn_out.json
  diff -u "$exp" /tmp/dynconn_out.json
  echo "PASS example $name (reference)"
done

echo "== stdin input =="
out=$(cat examples/request_basic.json | "$BIN")
[ "$out" = '{"ok":true,"answers":[true,false,false]}' ]
echo "PASS stdin"

echo "== malformed JSON exits with code 2 =="
code=0
echo '{invalid' | "$BIN" > /dev/null || code=$?
[ "$code" = "2" ]
echo "PASS malformed-json exit code"

echo "== schema error exits with code 2 =="
code=0
echo '{"n":"x","ops":[]}' | "$BIN" > /dev/null || code=$?
[ "$code" = "2" ]
echo "PASS schema-error exit code"

echo "ALL TESTS PASSED"
