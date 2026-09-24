#!/usr/bin/env bash
# End-to-end demo of the vector-clock multi-version register.
#
# It drives the same acceptance scenarios as the Go tests, over real HTTP:
#   1. isolated double writes on two partitions (replicas A and B)
#   2. duplicate / retransmitted message delivery (at-least-once transport)
#   3. two-way sync -> convergence with BOTH concurrent siblings retained
#   4. explicit merge with a version context
#   5. a late, stale pre-merge write arriving after the merge -> rejected
#   6. a third replica C concurrent with the merge -> merge does not erase it
#
# Usage:
#   go run . &            # starts the server on 127.0.0.1:18080
#   ./examples/demo.sh
#
# Requires: curl and jq. Override the address with BASE=http://host:port.
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:18080}"

section() { printf '\n==================== %s ====================\n' "$*"; }
ok()      { printf '%s\n' "$*"; }

# req METHOD PATH [JSON_BODY] — prints the response body, fails on HTTP error.
req() {
  local method=$1 path=$2 body=${3:-}
  local args=(-sS -X "$method" -H 'Content-Type: application/json' -w '\n%{http_code}')
  if [[ -n $body ]]; then
    args+=(-d "$body")
  fi
  args+=("$BASE$path")
  local raw status json
  raw=$(curl "${args[@]}")
  status=${raw##*$'\n'}
  json=${raw%$'\n'*}
  if (( status < 200 || status >= 300 )); then
    echo "$json" >&2
    echo "HTTP $status for $method $path" >&2
    exit 1
  fi
  echo "$json"
}

section "0. reset and create two partitioned replicas A and B"
req POST /replicas/reset
req POST /replicas '{"id":"A"}'
req POST /replicas '{"id":"B"}'
ok "replicas: $(req GET /replicas | jq -c .replicas)"

section "1. isolated double write: A and B cannot see each other"
VA=$(req PUT /replicas/A/keys/cfg '{"value":"from-A"}' | jq -r .written.id)
VB=$(req PUT /replicas/B/keys/cfg '{"value":"from-B"}' | jq -r .written.id)
ok "A wrote $VA ; B wrote $VB (clocks are incomparable -> concurrent)"
echo "-- A's view (expect 1 version, $VA):"
req GET /replicas/A/keys/cfg | jq '{clock, versions: [.versions[].id]}'
echo "-- B's view (expect 1 version, $VB):"
req GET /replicas/B/keys/cfg | jq '{clock, versions: [.versions[].id]}'

section "2. duplicate message: deliver A's write to B twice (retransmission)"
echo "-- first delivery (expect accepted):"
req POST /replicas/B/keys/cfg/messages \
  "$(jq -nc --arg id "$VA" '{versions:[{id:$id,origin:"A",clock:{A:1},value:"from-A"}]}')" \
  | jq -c .outcomes
echo "-- identical delivery again (expect duplicate, no new copy):"
req POST /replicas/B/keys/cfg/messages \
  "$(jq -nc --arg id "$VA" '{versions:[{id:$id,origin:"A",clock:{A:1},value:"from-A"}]}')" \
  | jq -c .outcomes
echo "-- B now holds the two concurrent siblings:"
req GET /replicas/B/keys/cfg | jq '[.versions[] | {id, value}]'

section "3. two-way sync A <-> B: convergence, neither sibling deleted"
req POST /replicas/A/sync '{"from":"A","to":"B","mode":"two-way"}' | jq .
echo "-- A after sync (expect A-1 and B-1):"
req GET /replicas/A/keys/cfg | jq '[.versions[] | {id, clock, value}]'
echo "-- B after sync (same set, same clocks):"
req GET /replicas/B/keys/cfg | jq '[.versions[] | {id, clock, value}]'

section "4. explicit merge ON A with context [$VA, $VB]"
MERGED=$(req POST /replicas/A/keys/cfg/merge \
  "$(jq -nc --arg v 'resolved=A+B' --argjson c "$(jq -nc --arg a "$VA" --arg b "$VB" '[$a,$b]')" \
    '{value:$v, context:$c}')" | jq -r .merged.id)
ok "merge produced version $MERGED; its clock dominates both context versions"
req GET /replicas/A/keys/cfg | jq '[.versions[] | {id, clock, value}]'

section "5. propagate merge A -> B (one-way), then a LATE STALE old write arrives"
req POST /replicas/A/sync '{"from":"A","to":"B","mode":"one-way"}' | jq .
echo "-- B after merge sync (expect only $MERGED):"
req GET /replicas/B/keys/cfg | jq '[.versions[] | {id, value}]'
echo "-- delayed pre-merge message $VB finally reaches B (expect superseded):"
req POST /replicas/B/keys/cfg/messages \
  "$(jq -nc '{versions:[{id:"B-1",origin:"B",clock:{B:1},value:"from-B"}]}')" \
  | jq -c .outcomes
echo "-- B still holds only the merge; the stale version was not resurrected:"
req GET /replicas/B/keys/cfg | jq '[.versions[] | {id, value}]'

section "6. outsider C wrote concurrently with the merge: merge must not erase C"
req POST /replicas '{"id":"C"}'
VC=$(req PUT /replicas/C/keys/cfg '{"value":"from-C"}' | jq -r .written.id)
req POST /replicas/A/sync '{"from":"A","to":"C","mode":"two-way"}' | jq .
echo "-- A and C converge to the merge $MERGED AND the concurrent write $VC:"
req GET /replicas/A/keys/cfg | jq '[.versions[] | {id, value}]'
req GET /replicas/C/keys/cfg | jq '[.versions[] | {id, value}]'

section "7. partial merge context: known-but-unmerged sibling must survive"
# Fresh key on three replicas: sync everyone together so A knows all three
# concurrent writes, then merge ONLY two of them on A.
V7_A=$(req PUT /replicas/A/keys/k7 '{"value":"v-A"}' | jq -r .written.id)
V7_B=$(req PUT /replicas/B/keys/k7 '{"value":"v-B"}' | jq -r .written.id)
V7_C=$(req PUT /replicas/C/keys/k7 '{"value":"v-C"}' | jq -r .written.id)
req POST /replicas/A/sync '{"from":"A","to":"B","mode":"two-way"}' >/dev/null
req POST /replicas/A/sync '{"from":"A","to":"C","mode":"two-way"}' >/dev/null
echo "-- A knows all three siblings before the merge:"
req GET /replicas/A/keys/k7 | jq '[.versions[].id]'
req POST /replicas/A/keys/k7/merge \
  "$(jq -nc --arg a "$V7_A" --arg b "$V7_B" '{value:"A+B-only", context:[$a,$b]}')" >/dev/null
echo "-- after merging only A's and B's versions, C's version stays as a sibling:"
req GET /replicas/A/keys/k7 | jq '[.versions[] | {id, value}]'

section "done"
