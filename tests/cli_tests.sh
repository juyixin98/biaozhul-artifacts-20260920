#!/usr/bin/env bash
# CLI integration tests for poly_locator.
set -u
BIN=./poly_locator
fail=0
checks=0

assert_eq() { # desc actual expected
  local desc="$1" actual="$2" expected="$3"
  checks=$((checks + 1))
  if [[ "$actual" == "$expected" ]]; then
    echo "  ok: $desc"
  else
    echo "  FAIL: $desc"
    echo "    expected: $expected"
    echo "    actual:   $actual"
    fail=$((fail + 1))
  fi
}

jget() { python3 -c "import json,sys; d=json.load(sys.stdin); print(eval(sys.argv[1]))" "$1"; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# 1. Basic locate via stdin.
cat > "$TMP/req.json" <<'JSON'
{
  "op": "locate",
  "compare_with_naive": true,
  "polygon": {
    "outer": [[0,0],[20,0],[20,20],[0,20]],
    "holes": [[[5,5],[15,5],[15,15],[5,15]]]
  },
  "points": [[2,2],[10,10],[10,5],[30,30]]
}
JSON
out=$("$BIN" - --compact < "$TMP/req.json")
echo "$out" | python3 -m json.tool >/dev/null || { echo "FAIL: invalid JSON output"; exit 1; }

locs=$(echo "$out" | python3 -c "
import json,sys
print(' '.join(r['location'] for r in json.load(sys.stdin)['results']))")
mismatches=$(echo "$out" | jget "d['counts']['naive_mismatches']")
assert_eq "4 query classifications" "$locs" "inside outside boundary outside"
assert_eq "naive mismatches reported" "$mismatches" "0"

# 2. CW outer + CCW hole accepted and reported.
cw=$(echo '{"op":"validate","polygon":{"outer":[[0,0],[0,20],[20,20],[20,0]],"holes":[[[5,5],[5,15],[15,15],[15,5]]]}}' | "$BIN" - --compact)
assert_eq "CW orientation status" "$(echo "$cw" | jget "d['status']")" "ok"
assert_eq "CW detected" "$(echo "$cw" | jget "d['meta']['outer_input_orientation']")" "cw"
assert_eq "hole ccw detected" "$(echo "$cw" | jget "d['meta']['holes_input_orientation'][0]")" "cw"

# 3. Invalid polygon (self-intersecting bowtie) -> invalid_polygon, exit 0.
set +e
echo '{"op":"locate","polygon":{"outer":[[0,0],[10,10],[10,0],[0,10]]},"points":[[1,1]]}' | \
  "$BIN" - --compact > "$TMP/bad.json"
rc=$?
set -e
assert_eq "invalid polygon status" "$(jget "d['status']" < "$TMP/bad.json")" "invalid_polygon"
assert_eq "invalid polygon code" "$(jget "d['error']['code']" < "$TMP/bad.json")" "SELF_INTERSECTING_RING"
assert_eq "invalid polygon exit code 0" "$rc" "0"

# 4. Malformed JSON -> exit 2.
set +e
echo '{nope' | "$BIN" - --compact > "$TMP/mal.json" 2>/dev/null
rc=$?
set -e
assert_eq "malformed json exit code" "$rc" "2"
assert_eq "malformed json code" "$(jget "d['error']['code']" < "$TMP/mal.json")" "MALFORMED_JSON"

# 5. Huge coordinates preserve exactness (all integral -> scale exponent 0;
#    exactness comes from arbitrary-precision integers, not decimal scaling).
huge=$("$BIN" examples/huge_coords_request.json --compact)
assert_eq "huge inside classification" \
  "$(echo "$huge" | jget "d['results'][0]['location']")" "inside"
assert_eq "huge outside classification" \
  "$(echo "$huge" | jget "d['results'][1]['location']")" "outside"
assert_eq "huge boundary classification" \
  "$(echo "$huge" | jget "d['results'][2]['location']")" "boundary"
assert_eq "huge scale exponent" \
  "$(echo "$huge" | jget "d['meta']['common_scale_exponent']")" "0"

# 6. prepare op.
prep=$(echo '{"op":"prepare","polygon":{"outer":[[0,0],[10,0],[10,10],[0,10]]}}' | "$BIN" - --compact)
assert_eq "prepare response" "$(echo "$prep" | jget "d['prepared']")" "True"

# 7. Area exactness for a fractional polygon.
area=$("$BIN" examples/fractional_request.json --compact)
assert_eq "fractional area" "$(echo "$area" | jget "d['polygon_stats']['area']")" "6.375"

echo ""
if [[ $fail -eq 0 ]]; then
  echo "CLI TESTS PASSED ($checks checks)"
else
  echo "CLI TESTS FAILED ($fail / $checks)"
  exit 1
fi
