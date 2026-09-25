#!/usr/bin/env bash
# e2e.sh — end-to-end CLI checks against the request samples.
# Requires build/simplify (run `make` first). Set VERBOSE=1 to show responses.
set -u
BIN=build/simplify
fail=0

assert() { # desc, condition-result (0 ok)
  if [ "$2" -eq 0 ]; then
    echo "ok   $1"
  else
    echo "FAIL $1"
    fail=1
  fi
}

[ -x "$BIN" ] || { make || { echo "build failed"; exit 1; }; }

# 1. Basic request: status ok, endpoints present, bound satisfied.
out=$("$BIN" samples/request_basic.json)
echo "$out" | grep -q '"status":"ok"'
assert "basic: status ok" $?
echo "$out" | grep -q '"bound_satisfied":true'
assert "basic: bound satisfied" $?
echo "$out" | grep -q '"kept_indices":\[0'
assert "basic: first endpoint kept" $?

# 2. Zero tolerance on an off-chord zigzag keeps all five points.
out=$("$BIN" samples/request_zero_tolerance.json)
echo "$out" | grep -q '"output_count":5'
assert "zero tolerance: 5 -> 5 points" $?
echo "$out" | grep -q '"max_distance":0'
assert "zero tolerance: max distance 0" $?

# 3. Backtracking polyline: fold tip kept and bound holds.
out=$("$BIN" samples/request_backtrack.json)
echo "$out" | grep -q '"bound_satisfied":true'
assert "backtrack: bound satisfied" $?
echo "$out" | grep -qE '"kept_indices":\[[0-9,]*5[0-9,]*\]'   # index 5 = fold tip (10,0)
assert "backtrack: fold tip survives" $?

# 4. Duplicate points + self intersection: bound holds, no NaN.
out=$("$BIN" samples/request_duplicates_and_self_intersection.json)
echo "$out" | grep -q '"bound_satisfied":true'
assert "duplicates/self-intersection: bound satisfied" $?
if echo "$out" | grep -qi 'nan'; then assert "no NaN in output" 1; else assert "no NaN in output" 0; fi

# 5. Invalid request: error JSON and non-zero exit.
"$BIN" samples/request_invalid.json >/dev/null
code=$?
[ "$code" -eq 1 ]
assert "invalid request: exit code 1" $?
err=$("$BIN" samples/request_invalid.json)
echo "$err" | grep -q '"status":"error"'
assert "invalid request: error JSON" $?

# 6. stdin works and output is deterministic (byte-identical twice).
a=$("$BIN" < samples/request_basic.json)
b=$("$BIN" < samples/request_basic.json)
[ "$a" = "$b" ]
assert "stdin + deterministic bytes" $?

# 7. Pretty mode is valid-looking (newlines present).
"$BIN" --pretty samples/request_basic.json | grep -q $'\n'
assert "pretty mode emits newlines" $?

[ "${VERBOSE:-0}" = 1 ] && "$BIN" --pretty samples/request_basic.json

if [ "$fail" -eq 0 ]; then echo "ALL E2E CHECKS PASSED"; else echo "E2E FAILURES PRESENT"; fi
exit $fail
