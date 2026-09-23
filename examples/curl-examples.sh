#!/usr/bin/env bash
# End-to-end request examples for the Raft lab HTTP control plane.
#
# Start a server first in another terminal:
#   go run ./cmd/server -addr 127.0.0.1:8080 -nodes 3 -tick-ms 50
#
# Then: bash examples/curl-examples.sh
set -euo pipefail

B="${B:-http://127.0.0.1:8080}"
say() { printf '\n=== %s ===\n' "$1"; }

say "health"
curl -s "$B/v1/health"; echo

say "cluster overview (wait for a leader if tick-ms>0)"
sleep 1
curl -s "$B/v1/cluster"; echo

say "elect a leader in manual mode (no-op when tick-ms>0)"
curl -s -X POST "$B/v1/tick" -d '{"n":25}'; echo

say "propose through the current leader"
curl -s -X POST "$B/v1/propose" -d '{"command":"SET color blue"}'; echo
sleep 0.5

say "propose two more commands"
curl -s -X POST "$B/v1/propose" -d '{"command":"SET a 1"}' >/dev/null
curl -s -X POST "$B/v1/propose" -d '{"command":"SET b 2"}' >/dev/null
sleep 0.5

say "node state + key/value machine"
curl -s "$B/v1/nodes"; echo

say "find the leader and isolate it (printed for clarity)"
LEADER=$(curl -s "$B/v1/cluster" | sed -n 's/.*"leaderId": \([0-9-]*\).*/\1/p' | head -1)
echo "isolating leader n$LEADER"
curl -s -X POST "$B/v1/nodes/$LEADER/partition"; echo

say "the stale leader still accepts locally, but cannot commit"
curl -s -X POST "$B/v1/propose" -d "{\"node\":$LEADER,\"command\":\"SET ghost stale\"}"; echo
sleep 1

say "meanwhile the majority elects a new-term leader"
curl -s "$B/v1/cluster"; echo

say "heal connectivity and release every delayed/old message"
curl -s -X POST "$B/v1/heal"; echo
curl -s -X POST "$B/v1/stale/release"; echo
sleep 1

say "ghost entry must never be committed; new proposals overwrite it"
curl -s -X POST "$B/v1/propose" -d '{"command":"SET c 3"}' >/dev/null
sleep 0.5
curl -s "$B/v1/cluster"; echo

say "stop/start a node (state restored from storage)"
TARGET=1
curl -s -X POST "$B/v1/nodes/$TARGET/stop"; echo
curl -s -X POST "$B/v1/nodes/$TARGET/start"; echo
sleep 0.8
curl -s "$B/v1/nodes/$TARGET"; echo

say "enumerate short fault traces on the correct implementation"
curl -s -X POST "$B/v1/enumerate" -d '{"size":3,"maxDepth":3,"maxTraces":4000,"ticksPerStep":4}'; echo

say "deterministic Figure 8: correct variant passes"
curl -s "$B/v1/figure8?buggy=false"; echo

say "deterministic Figure 8: buggy variant returns HTTP 409 + counterexample"
curl -s -w '\nHTTP %{http_code}\n' "$B/v1/figure8?buggy=true"

say "replay a custom JSON trace on a fresh in-memory 3-node cluster"
curl -s -X POST "$B/v1/replay" -d '{
  "size": 3,
  "actions": [
    {"op":"run","node":25},
    {"op":"proposeLeader","command":"SET k1 v1"},
    {"op":"run","node":8},
    {"op":"partition","node":1},
    {"op":"run","node":20},
    {"op":"heal"},
    {"op":"releaseStale"},
    {"op":"run","node":20}
  ]
}'; echo
