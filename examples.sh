#!/usr/bin/env bash
# End-to-end demo of the Raft lab over its HTTP API.
# Requires: a running server (go run ./cmd/raftlab) and curl + python3.
#
# Usage:
#   go run ./cmd/raftlab -addr :18080 &
#   ./examples.sh 127.0.0.1:18080
set -euo pipefail

cd "$(dirname "$0")"
BASE="http://${1:-127.0.0.1:8080}"
FMT="python3 examples_fmt.py"
section() { echo; echo "===== $* ====="; }

section "1. Create a 3-node standard Raft cluster (deterministic seed 99)"
SID=$(curl -sS -X POST "$BASE/sessions" \
  -d '{"nodes":3,"variant":"standard","seed":99}' | $FMT field id)
echo "session: $SID"

section "2. Advance virtual time 250ms -> exactly one leader is elected"
curl -sS -X POST "$BASE/sessions/$SID/advance" -d '{"ms":250}' | $FMT field leader

section "3. Propose SET k v1 (routed to the current leader) and replicate"
curl -sS -X POST "$BASE/sessions/$SID/propose" -d '{"command":"SET k v1"}' > /tmp/raftlab-prop.json
echo -n "accepted: "; $FMT field accepted < /tmp/raftlab-prop.json
echo -n "leader node: "; $FMT field node < /tmp/raftlab-prop.json
curl -sS -X POST "$BASE/sessions/$SID/advance" -d '{"ms":80}' | $FMT nodes

section "4. Partition the leader away; the majority elects a new-term leader"
curl -sS -X POST "$BASE/sessions/$SID/partition" -d '{"isolate":[2]}' >/dev/null
curl -sS -X POST "$BASE/sessions/$SID/advance" -d '{"ms":300}' | $FMT leader-nodes

section "5. Heal the partition; the deposed leader steps down and catches up"
curl -sS -X POST "$BASE/sessions/$SID/heal" >/dev/null
curl -sS -X POST "$BASE/sessions/$SID/advance" -d '{"ms":300}' | $FMT nodes

section "6. Fetch and replay a counterexample: delayed stale message (buggy 'notermcheck')"
curl -sS "$BASE/scenarios/counterexamples" \
  | $FMT field counterexamples 0 > /tmp/raftlab-ce0.json
curl -sS -X POST "$BASE/scenarios/run" -d @/tmp/raftlab-ce0.json | $FMT ce

section "7. Replay the SAME fault trace against standard Raft -> invariants hold"
python3 -c 'import json; sc=json.load(open("/tmp/raftlab-ce0.json")); sc["variant"]="standard"; json.dump(sc,open("/tmp/raftlab-ce0-standard.json","w"))'
curl -sS -X POST "$BASE/scenarios/run" -d @/tmp/raftlab-ce0-standard.json | $FMT field ok

section "8. Enumerate short fault traces against the standard implementation"
curl -sS -X POST "$BASE/enumerate" \
  -d '{"variant":"standard","depth":2,"fuzz":300,"maxCounterexamples":5}' | $FMT enum

echo
echo "All demo steps completed."
