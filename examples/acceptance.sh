#!/usr/bin/env bash
# End-to-end acceptance test for the resumable behavior-tree backend.
# Requires: running server on ${BASE_URL} (default http://localhost:8080)
# and curl + python3. Every check prints PASS/FAIL; exit code reflects result.
set -uo pipefail

BASE_URL="${BASE_URL:-http://localhost:8080}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FAILS=0

say()  { printf '\033[1;36m== %s\033[0m\n' "$*"; }
pass() { printf '  \033[0;32mPASS\033[0m %s\n' "$*"; }
fail() { printf '  \033[0;31mFAIL\033[0m %s\n' "$*"; FAILS=$((FAILS+1)); }

# jget KEY: read a JSON key from stdin via python3 (no jq dependency).
jget() { python3 -c "import json,sys; print(json.load(sys.stdin)$1)"; }

# jcontains NEEDLE: assert the response body on stdin contains NEEDLE.
jcontains() { grep -q "$1" && pass "$2" || fail "$2 (missing: $1)"; }

# wait_status EXEC_ID WANT [seconds]
wait_status() {
  local id="$1" want="$2" deadline=$(( ${3:-10} * 10 ))
  for _ in $(seq 1 "$deadline"); do
    local st
    st=$(curl -s "$BASE_URL/executions/$id" | jget "['status']")
    [ "$st" = "$want" ] && return 0
    sleep 0.1
  done
  return 1
}

# ---------------------------------------------------------------- health
say "health"
curl -sf "$BASE_URL/healthz" | jcontains '"ok"' "healthz responds ok" || true

# ------------------------------------------------- 1. immutability & dedup
say "publish immutability: identical content -> same version, changed -> new version"
PUB1=$(curl -s -X POST "$BASE_URL/trees/acc_seq/versions" -d @"$HERE/01_sequence.json")
V1=$(echo "$PUB1" | jget "['version']")
[ "$V1" = "1" ] && pass "first publish is version 1" || fail "first publish version=$V1"
PUB2=$(curl -s -X POST "$BASE_URL/trees/acc_seq/versions" -d @"$HERE/01_sequence.json")
echo "$PUB2" | jcontains '"created":false' "republish identical JSON is idempotent (created=false)" || true

# ------------------------------------------------- 2. sequence + real hash
say "sequence runs and hash.sha256 really computes a digest"
E=$(curl -s -X POST "$BASE_URL/executions" -d '{"tree":"acc_seq"}' | jget "['execution_id']")
curl -sf -X POST "$BASE_URL/executions/$E/tick" -d '{"note":"acceptance"}' >/dev/null \
  && pass "tick accepted" || fail "tick rejected"
wait_status "$E" success && pass "sequence execution -> success" || fail "sequence did not succeed"
SNAP=$(curl -s "$BASE_URL/executions/$E/snapshot")
echo "$SNAP" | jcontains 0c80596709d6940d6da76751a5a1bdda41eabf38c95de2c95ffb66ef0a889c86 \
  "real SHA-256 of 'hello-resumable-behavior-tree'" || true
echo "$SNAP" | jcontains '"dispatches":1' "each action dispatched exactly once" || true

# ------------------------------------------------- 3. fallback branches
say "fallback walks failing branches until one succeeds"
curl -sf -X POST "$BASE_URL/trees/acc_fb/versions" -d @"$HERE/02_fallback.json" >/dev/null
E=$(curl -s -X POST "$BASE_URL/executions" -d '{"tree":"acc_fb"}' | jget "['execution_id']")
curl -sf -X POST "$BASE_URL/executions/$E/tick" -d '{}' >/dev/null
wait_status "$E" success && pass "fallback -> success via third branch" || fail "fallback failed"
curl -s "$BASE_URL/executions/$E/snapshot" \
  | jcontains '"node_id":"unreliable","status":"failure"' "first branch recorded failure" || true

# ------------------------------------------------- 4. parallel threshold
say "parallel threshold: 2 successes win, slow sibling canceled"
curl -sf -X POST "$BASE_URL/trees/acc_par/versions" -d @"$HERE/03_parallel_threshold.json" >/dev/null
E=$(curl -s -X POST "$BASE_URL/executions" -d '{"tree":"acc_par"}' | jget "['execution_id']")
curl -sf -X POST "$BASE_URL/executions/$E/tick" -d '{}' >/dev/null
wait_status "$E" success && pass "parallel -> success at threshold 2" || fail "parallel did not succeed"
sleep 0.2
if curl -s "$BASE_URL/executions/$E/snapshot" | python3 -c "
import json,sys
d=json.load(sys.stdin)
inv={i['node_id']: i['status'] for i in d['invocations']}
assert inv.get('fast_a')=='success' and inv.get('fast_b')=='success', inv
assert inv.get('slow_c')=='canceled', inv
"; then
  pass "slow sibling invocation canceled while 2 fast children succeeded"
else
  fail "parallel cancellation state incorrect"
fi

# ------------------------------------------------- 5. timeout vs success race
say "timeout races"
curl -sf -X POST "$BASE_URL/trees/acc_race/versions" -d @"$HERE/04_timeout_success_race.json" >/dev/null
E=$(curl -s -X POST "$BASE_URL/executions" -d '{"tree":"acc_race"}' | jget "['execution_id']")
curl -sf -X POST "$BASE_URL/executions/$E/tick" -d '{}' >/dev/null
wait_status "$E" success && pass "fast success beats 150ms timeout" || fail "success lost the race"

curl -sf -X POST "$BASE_URL/trees/acc_to/versions" -d @"$HERE/05_timeout_wins.json" >/dev/null
E=$(curl -s -X POST "$BASE_URL/executions" -d '{"tree":"acc_to"}' | jget "['execution_id']")
curl -sf -X POST "$BASE_URL/executions/$E/tick" -d '{}' >/dev/null
wait_status "$E" failure && pass "80ms timeout fails a 5s action" || fail "timeout did not fire"
sleep 0.6
curl -s "$BASE_URL/executions/$E" | jcontains '"status":"failure"' \
  "late result from timed-out worker does not resurrect the tree" || true

# ------------------------------------------------- 6. cancel + conflict
say "execution cancel"
E=$(curl -s -X POST "$BASE_URL/executions" -d '{"tree":"acc_to"}' | jget "['execution_id']")
curl -sf -X POST "$BASE_URL/executions/$E/tick" -d '{}' >/dev/null
sleep 0.02
CODE=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE_URL/executions/$E/cancel")
[ "$CODE" = "200" ] && pass "cancel returns 200" || fail "cancel returned $CODE"
CODE=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE_URL/executions/$E/tick" -d '{}')
[ "$CODE" = "409" ] && pass "tick after cancel rejected with 409" || fail "post-cancel tick returned $CODE"

# ------------------------------------------------- 7. error handling
say "error cases"
CODE=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE_URL/trees/x/versions" \
  -d '{"root":"missing","nodes":{}}')
[ "$CODE" = "400" ] && pass "invalid tree rejected with 400" || fail "invalid tree returned $CODE"
CODE=$(curl -s -o /dev/null -w '%{http_code}' "$BASE_URL/executions/00000000-0000-0000-0000-000000000000")
[ "$CODE" = "404" ] && pass "unknown execution -> 404" || fail "unknown execution returned $CODE"

echo
if [ "$FAILS" = "0" ]; then
  printf '\033[0;32mALL ACCEPTANCE CHECKS PASSED\033[0m\n'
  exit 0
fi
printf '\033[0;31m%d CHECK(S) FAILED\033[0m\n' "$FAILS"
exit 1
