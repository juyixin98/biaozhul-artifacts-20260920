#!/usr/bin/env bash
#
# integration.sh — black-box tests against the rect_union binary.
# Covers CLI stdin/file modes, every sample, degenerate handling, error
# paths and (if curl exists) the HTTP entry point.
#
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BIN="$ROOT/build/rect_union"
PASS=0
FAIL=0

ok()   { printf '  [ ok ] %s\n' "$1"; PASS=$((PASS + 1)); }
bad()  { printf '  [FAIL] %s\n' "$1"; FAIL=$((FAIL + 1)); }

# Extract an integer JSON field with python3 when available; fall back to
# a tiny grep-based extractor (works for the flat/known responses here).
json_field() {
    local file="$1" key="$2"
    if command -v python3 >/dev/null 2>&1; then
        python3 -c '
import json,sys
d=json.load(open(sys.argv[1]))
key=sys.argv[2]
cur=d
for part in key.split("."):
    cur=cur[part]
print(json.dumps(cur) if isinstance(cur,(list,dict)) else cur)
' "$file" "$key"
    else
        grep -o "\"$key\":[0-9-]*" "$file" | head -1 | cut -d: -f2
    fi
}

# assert_metrics <case-name> <input-file> <exp-area> <exp-perim>
assert_metrics() {
    local name="$1" input="$2" exp_area="$3" exp_perim="$4"
    local out
    out="$("$BIN" --file "$input" 2>/dev/null)" || { bad "$name (nonzero exit on valid input)"; return; }
    local got_area got_perim
    printf '%s' "$out" > /tmp/ru_out.json
    got_area="$(json_field /tmp/ru_out.json area)"
    got_perim="$(json_field /tmp/ru_out.json perimeter)"
    if [[ "$got_area" == "$exp_area" && "$got_perim" == "$exp_perim" ]]; then
        ok "$name -> area=$got_area perimeter=$got_perim"
    else
        bad "$name (got area=$got_area perimeter=$got_perim, want $exp_area/$exp_perim)"
        printf '       response: %s\n' "$out"
    fi
}

assert_exit_nonzero() {
    local name="$1" input="$2"
    if "$BIN" --file "$input" >/dev/null 2>&1; then
        bad "$name (expected nonzero exit)"
    else
        ok "$name (rejected with nonzero exit)"
    fi
}

assert_stdin() {
    local name="$1" exp_area="$2" exp_perim="$3"
    local out
    out="$(printf '%s' '{"rectangles":[{"x1":0,"y1":0,"x2":1,"y2":1}]}' | "$BIN")" || { bad "$name (stdin)"; return; }
    printf '%s' "$out" > /tmp/ru_out.json
    local a p
    a="$(json_field /tmp/ru_out.json area)"
    p="$(json_field /tmp/ru_out.json perimeter)"
    if [[ "$a" == "$exp_area" && "$p" == "$exp_perim" ]]; then ok "$name"; else bad "$name ($a/$p)"; fi
}

printf '== CLI: valid samples ==\n'
assert_metrics "minimal single square"              "$ROOT/samples/minimal.json"                  4   8
assert_metrics "overlap sample"                     "$ROOT/samples/overlap.json"                  11  18
assert_metrics "adjacent + 3 degenerate"            "$ROOT/samples/adjacent_and_degenerate.json"  8   12
assert_metrics "nested + side-neighbor"             "$ROOT/samples/nested.json"                   175 60
assert_metrics "same-x multi-event staircase"       "$ROOT/samples/same_x_events.json"            16  18
assert_stdin   "stdin single square"                1 4

printf '== CLI: degenerate accounting ==\n'
out="$("$BIN" --file "$ROOT/samples/adjacent_and_degenerate.json")"
printf '%s' "$out" > /tmp/ru_out.json
ignored="$(json_field /tmp/ru_out.json ignored_rectangles | tr -d '[:space:]')"
if [[ "$ignored" == *'"index":2'* && "$ignored" == *'"index":3'* && "$ignored" == *'"index":4'* ]]; then
    ok "three degenerate rects reported at indices 2,3,4"
else
    bad "degenerate accounting: $ignored"
fi

printf '== CLI: error paths ==\n'
assert_exit_nonzero "fractional coordinate"      "$ROOT/samples/invalid_fraction.json"

printf 'not json' > /tmp/ru_bad.json
assert_exit_nonzero "invalid JSON"               /tmp/ru_bad.json

printf '{}' > /tmp/ru_bad.json
assert_exit_nonzero "missing rectangles"         /tmp/ru_bad.json

printf '{"rectangles":[{"x1":2,"y1":0,"x2":0,"y2":2}]}' > /tmp/ru_bad.json
assert_exit_nonzero "inverted corners"           /tmp/ru_bad.json

printf '{"rectangles":[{"x1":0,"y1":0,"x2":2,"y2":2000000000}]}' > /tmp/ru_bad.json
assert_exit_nonzero "coordinate out of range"    /tmp/ru_bad.json

printf '{"rectangles":[]}' | "$BIN" | grep -q '"area":0' \
    && ok "empty array -> 0" || bad "empty array"

printf '== HTTP entry point (exercised when curl is available) ==\n'
if command -v curl >/dev/null 2>&1; then
    PORT=18084
    "$BIN" serve --port "$PORT" --host 127.0.0.1 >/tmp/ru_server.log 2>&1 &
    SRV_PID=$!
    for _ in $(seq 1 50); do
        curl -s "http://127.0.0.1:$PORT/healthz" | grep -q healthy && break
        sleep 0.1
    done

    health="$(curl -s "http://127.0.0.1:$PORT/healthz")"
    printf '%s' "$health" | grep -q '"status":"healthy"' && ok "GET /healthz" || bad "GET /healthz: $health"

    resp="$(curl -s -X POST "http://127.0.0.1:$PORT/union" \
        -H 'Content-Type: application/json' \
        --data '{"rectangles":[{"x1":0,"y1":0,"x2":2,"y2":2},{"x1":2,"y1":0,"x2":4,"y2":2}]}')"
    printf '%s' "$resp" > /tmp/ru_out.json
    a="$(json_field /tmp/ru_out.json area)"; p="$(json_field /tmp/ru_out.json perimeter)"
    [[ "$a" == 8 && "$p" == 12 ]] && ok "POST /union adjacent pair (8/12)" || bad "POST /union: $resp"

    code="$(curl -s -o /tmp/ru_out.json -w '%{http_code}' -X POST "http://127.0.0.1:$PORT/union" \
        --data 'nope')"
    [[ "$code" == 400 ]] && ok "POST invalid JSON -> 400" || bad "POST invalid JSON -> $code"

    code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/nope")"
    [[ "$code" == 404 ]] && ok "GET /nope -> 404" || bad "GET /nope -> $code"

    code="$(curl -s -o /dev/null -w '%{http_code}' -X GET "http://127.0.0.1:$PORT/union")"
    [[ "$code" == 405 ]] && ok "GET /union -> 405" || bad "GET /union -> $code"

    kill "$SRV_PID" 2>/dev/null
    wait "$SRV_PID" 2>/dev/null
else
    ok "curl not found — HTTP checks skipped"
fi

printf '\n== integration: %d passed, %d failed ==\n' "$PASS" "$FAIL"
[[ "$FAIL" == 0 ]]
