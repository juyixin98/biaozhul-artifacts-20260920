#!/usr/bin/env bash
# End-to-end API tests: boots the real server and exercises it over HTTP with curl.
# Outputs PASS/FAIL lines; exits non-zero if any check fails.
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${DOM_TEST_PORT:-18080}"
BASE="http://127.0.0.1:${PORT}"
SERVER="$ROOT/build/dom-server"
FAILURES=0

say()  { printf '%s\n' "$*"; }
pass() { say "PASS: $*"; }
fail() { say "FAIL: $*"; FAILURES=$((FAILURES + 1)); }

[ -x "$SERVER" ] || { echo "building server..."; make -C "$ROOT" "$SERVER" >/dev/null || exit 1; }

"$SERVER" --port "$PORT" >/tmp/dom-server-test.log 2>&1 &
SRV_PID=$!
cleanup() { kill "$SRV_PID" 2>/dev/null; wait "$SRV_PID" 2>/dev/null; }
trap cleanup EXIT

# Wait for readiness (max ~5s).
for _ in $(seq 1 50); do
  if curl -sf "$BASE/healthz" >/dev/null 2>&1; then ready=1; break; fi
  sleep 0.1
done
if [ "${ready:-0}" != "1" ]; then
  echo "server did not become ready; log:"; cat /tmp/dom-server-test.log; exit 1
fi

post() { # file -> sets HTTP_STATUS and BODY
  local file="$1"
  local out
  out=$(curl -s -w '\n%{http_code}' -X POST -H 'Content-Type: application/json' \
        --data-binary "@$file" "$BASE$2")
  HTTP_STATUS="${out##*$'\n'}"
  BODY="${out%$'\n'*}"
}

post_raw() { # raw body string, path
  local data="$1"
  local out
  out=$(curl -s -w '\n%{http_code}' -X POST -H 'Content-Type: application/json' \
        --data "$data" "$BASE$2")
  HTTP_STATUS="${out##*$'\n'}"
  BODY="${out%$'\n'*}"
}

# python assertion over $BODY; $1 = python expression (body bound to `j`)
assertPy() {
  local desc="$1"; shift
  BODY="$BODY" python3 - "$@" <<'PY'
import json, os, sys
try:
    j = json.loads(os.environ["BODY"])
except Exception as e:
    print(f"JSON-PARSE-ERROR: {e}"); sys.exit(2)
expr = sys.argv[1]
try:
    ok = eval(expr, {"j": j})
except Exception as e:
    print(f"EXPR-ERROR: {e}"); sys.exit(2)
sys.exit(0 if ok else 1)
PY
  local rc=$?
  if [ $rc -eq 0 ]; then pass "$desc"; else fail "$desc (body: ${BODY:0:300})"; fi
}

# 1. health ---------------------------------------------------------------
code=$(curl -s -o /tmp/health.json -w '%{http_code}' "$BASE/healthz")
[ "$code" = "200" ] && pass "healthz returns 200" || fail "healthz status $code"
grep -q '"ok": true' /tmp/health.json && pass "healthz body ok" || fail "healthz body: $(cat /tmp/health.json)"

# 2. main CFG: back edge, multi exit, unreachable cycle --------------------
post "$ROOT/examples/cfg_fault_location.json" /api/analyze
[ "$HTTP_STATUS" = "200" ] && pass "analyze cfg_fault_location 200" || fail "analyze status $HTTP_STATUS: $BODY"
echo "$BODY" > /tmp/analyze_main.json
assertPy "main: overall ok"                         'j["ok"] is True'
assertPy "main: reachable set excludes U1/U2/U3"    'j["reachable"]==["B0","B1","B2","B3","B4","B5","B6","B7"]'
assertPy "main: unreachable = U1,U2,U3"             'sorted(j["unreachable"])==["U1","U2","U3"]'
assertPy "main: idom(B0)=null (entry)"              'next(n for n in j["nodes"] if n["label"]=="B0")["idom"] is None'
assertPy "main: idom(B1)=B0"                        'next(n for n in j["nodes"] if n["label"]=="B1")["idom"]=="B0"'
assertPy "main: idom(B2)=B1 (loop header)"          'next(n for n in j["nodes"] if n["label"]=="B2")["idom"]=="B1"'
assertPy "main: idom(B3)=B2"                        'next(n for n in j["nodes"] if n["label"]=="B3")["idom"]=="B2"'
assertPy "main: idom(B4)=B2 (converges at B2)"      'next(n for n in j["nodes"] if n["label"]=="B4")["idom"]=="B2"'
assertPy "main: idom(B5)=B2 (paths via B3/B4)"      'next(n for n in j["nodes"] if n["label"]=="B5")["idom"]=="B2"'
assertPy "main: idom(B6)=B2"                        'next(n for n in j["nodes"] if n["label"]=="B6")["idom"]=="B2"'
assertPy "main: idom(B7)=B5"                        'next(n for n in j["nodes"] if n["label"]=="B7")["idom"]=="B5"'
assertPy "main: DF(B2)=[B2] (back edge B3->B2)" 'next(n for n in j["nodes"] if n["label"]=="B2")["dominanceFrontier"]==["B2"]'
assertPy "main: DF(B3)=[B2,B5] (latch + exit)"   'next(n for n in j["nodes"] if n["label"]=="B3")["dominanceFrontier"]==["B2","B5"]'
assertPy "main: DF(B4)=[B5,B6]"                     'next(n for n in j["nodes"] if n["label"]=="B4")["dominanceFrontier"]==["B5","B6"]'
assertPy "main: DF(B5)=[B6]"                        'next(n for n in j["nodes"] if n["label"]=="B5")["dominanceFrontier"]==["B6"]'
assertPy "main: unreachable nodes have empty DF"    'all(next(n for n in j["nodes"] if n["label"]==u)["dominanceFrontier"]==[] for u in ("U1","U2","U3"))'
assertPy "main: unreachable idom null"              'all(next(n for n in j["nodes"] if n["label"]==u)["idom"] is None for u in ("U1","U2","U3"))'
assertPy "main: dominators(B7) chain"               'next(n for n in j["nodes"] if n["label"]=="B7")["dominators"]==["B0","B1","B2","B5","B7"]'
assertPy "main: reference naive agrees on idom"     'j["references"]["idomAgreesWithNaive"] is True'
assertPy "main: reference naive agrees on frontier" 'j["references"]["frontierAgreesWithNaive"] is True'
assertPy "main: reference domsets agree"            'j["references"]["domSetsAgreeWithNaive"] is True'
assertPy "main: reference path-enum agrees"         'j["references"]["domSetsAgreeWithPathEnum"] is True'
assertPy "main: path enum actually ran"             'j["references"]["simplePathEnumeration"] is True and j["references"]["totalSimplePathsFromEntry"]>0'
assertPy "main: idom tree edges span reachables"    'len(j["idomTreeEdges"])==j["graph"]["reachableCount"]-1'

# 3. diamond + multi exit --------------------------------------------------
post "$ROOT/examples/diamond_multi_exit.json" /api/analyze
[ "$HTTP_STATUS" = "200" ] && pass "analyze diamond 200" || fail "diamond status $HTTP_STATUS"
assertPy "diamond: idom(D)=A"      'next(n for n in j["nodes"] if n["label"]=="D")["idom"]=="A"'
assertPy "diamond: DF(B)=[D]"      'next(n for n in j["nodes"] if n["label"]=="B")["dominanceFrontier"]==["D"]'
assertPy "diamond: idom(E)=D"      'next(n for n in j["nodes"] if n["label"]=="E")["idom"]=="D"'

# 4. irreducible loop -------------------------------------------------------
post "$ROOT/examples/irreducible_loop.json" /api/analyze
[ "$HTTP_STATUS" = "200" ] && pass "analyze irreducible 200" || fail "irreducible status $HTTP_STATUS"
assertPy "irreducible: idom(X)=S (not S via fixed header)" 'next(n for n in j["nodes"] if n["label"]=="X")["idom"]=="S"'
assertPy "irreducible: idom(Y)=S" 'next(n for n in j["nodes"] if n["label"]=="Y")["idom"]=="S"'
assertPy "irreducible: idom(Z)=S" 'next(n for n in j["nodes"] if n["label"]=="Z")["idom"]=="S"'
assertPy "irreducible: references agree" 'j["references"]["domSetsAgreeWithPathEnum"] is True'

# 5. dominates queries ------------------------------------------------------
post "$ROOT/examples/dominates_query.json" /api/dominates
[ "$HTTP_STATUS" = "200" ] && pass "dominates 200" || fail "dominates status $HTTP_STATUS: $BODY"
q() { echo "$BODY" | python3 -c "import json,sys; rs=json.load(sys.stdin)['results']; print([ (r['dominates'],r.get('properlyDominates')) for r in rs if r['a']=='$1' and r['b']=='$2'][0])"; }
[ "$(q B0 B5)" = "(True, True)" ]    && pass "query B0 dom B5 = (T,T)"        || fail "query B0 dom B5 got $(q B0 B5)"
[ "$(q B1 B4)" = "(True, True)" ]    && pass "query B1 dom B4 = (T,T)"        || fail "query B1 dom B4 got $(q B1 B4)"
[ "$(q B2 B4)" = "(False, False)" ]  && pass "query B2 dom B4 = (F,F)"        || fail "query B2 dom B4 got $(q B2 B4)"
[ "$(q B3 B4)" = "(False, False)" ]  && pass "query B3 dom B4 = (F,F)"        || fail "query B3 dom B4 got $(q B3 B4)"
[ "$(q B4 B4)" = "(True, False)" ]   && pass "query B4 dom B4 reflexive only" || fail "query B4 dom B4 got $(q B4 B4)"
[ "$(q B0 U1)" = "(False, False)" ]  && pass "query reachable vs unreachable false" || fail "query B0 U1 got $(q B0 U1)"
[ "$(q U1 U2)" = "(False, False)" ]  && pass "query among unreachable false"  || fail "query U1 U2 got $(q U1 U2)"
assertPy "query unknown node flagged" 'any(r["a"]=="B2" and r["b"]=="ZZZ" and r["known"] is False for r in j["results"])'

# 6. error handling ---------------------------------------------------------
post "$ROOT/examples/error_unknown_entry.json" /api/analyze
[ "$HTTP_STATUS" = "422" ] && pass "unknown entry -> 422" || fail "unknown entry status $HTTP_STATUS"
assertPy "error body ok=false with message" 'j["ok"] is False and "entry" in j["error"]'

post_raw '{ not json' /api/analyze
[ "$HTTP_STATUS" = "400" ] && pass "invalid JSON -> 400" || fail "invalid JSON status $HTTP_STATUS"

post_raw '{"entry":"A","nodes":["A"],"edges":[["A","B"]]}' /api/analyze
[ "$HTTP_STATUS" = "422" ] && pass "edge to unknown node -> 422" || fail "edge unknown status $HTTP_STATUS"

code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/nope")
[ "$code" = "404" ] && pass "unknown path -> 404" || fail "unknown path status $code"
code=$(curl -s -o /dev/null -w '%{http_code}' -X GET "$BASE/api/analyze")
[ "$code" = "405" ] && pass "GET on POST endpoint -> 405" || fail "method status $code"

echo
if [ "$FAILURES" -eq 0 ]; then
  say "ALL API TESTS PASSED"
  exit 0
fi
say "$FAILURES API TEST(S) FAILED"
exit 1
