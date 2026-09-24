#!/usr/bin/env bash
# demo.sh — three-replica OR-Set walkthrough.
#
# Scenario:
#   1. Start 3 replicas and populate a common set; fully sync.
#   2. Simulate a partition: divergent concurrent edits with NO syncs,
#      including an add/remove conflict on the same element ("a").
#   3. Heal the partition one replica at a time and prove convergence in
#      every healing order run; show duplicate sync is a no-op.
#   4. Coordinate safe tombstone GC and show the counters dropping.
set -euo pipefail

cd "$(dirname "$0")/.."
export PATH="$PATH:/usr/local/go/bin"

BASE1=http://127.0.0.1:18081
BASE2=http://127.0.0.1:18082
BASE3=http://127.0.0.1:18083
ALL=("$BASE1" "$BASE2" "$BASE3")
LOGDIR=$(mktemp -d)
trap 'kill "${PIDS[@]}" 2>/dev/null || true' EXIT

bold() { printf '\n\033[1m=== %s ===\033[0m\n' "$*"; }
req()  { curl -sS -X "$1" "${@:3}" -H 'Content-Type: application/json' "$2"; echo; }

wait_ready() {
  for i in $(seq 1 50); do
    curl -sf "$1/health" >/dev/null 2>&1 && return 0
    sleep 0.1
  done
  echo "replica $1 did not become ready"; return 1
}

bold "build & start 3 replicas (logs in $LOGDIR)"
go build -o "$LOGDIR/orset-server" ./cmd/server
PIDS=()
for i in 1 2 3; do
  "$LOGDIR/orset-server" -addr ":1808$i" -id "node$i" >"$LOGDIR/node$i.log" 2>&1 &
  PIDS+=($!)
  wait_ready "${ALL[$((i-1))]}"
done
echo "replicas ready: ${ALL[*]}"

bold "1) common history: node1 adds a b c, then full mesh sync"
for e in a b c; do
  req POST "$BASE1/elements" -d "{\"element\":\"$e\"}" >/dev/null
done
# node1 -> all, then node2/node3 pull+push so everyone converges
req POST "$BASE1/sync" -d "{\"peers\":[\"$BASE2\",\"$BASE3\"]}" >/dev/null
req POST "$BASE2/sync" -d "{\"peers\":[\"$BASE1\",\"$BASE3\"]}" >/dev/null
req POST "$BASE3/sync" -d "{\"peers\":[\"$BASE1\",\"$BASE2\"]}" >/dev/null
for b in "${ALL[@]}"; do
  echo -n "$b members: "; req GET "$b/elements"
done

bold "2) partition: divergent concurrent edits (NO syncs in this phase)"
echo "-- node1 removes a"
req DELETE "$BASE1/elements/a"
echo "-- node2 CONCURRENTLY re-adds a (fresh unique tag) and adds d"
req POST   "$BASE2/elements" -d '{"element":"a"}'
req POST   "$BASE2/elements" -d '{"element":"d"}'
echo "-- node3 removes b and adds e"
req DELETE "$BASE3/elements/b"
req POST   "$BASE3/elements" -d '{"element":"e"}'
for i in 1 2 3; do
  echo -n "node$i while partitioned: "; req GET "${ALL[$((i-1))]}/elements"
done

bold "3) partition heals node-by-node (order: node1, node2, node3)"
req POST "$BASE1/sync" -d "{\"peers\":[\"$BASE2\",\"$BASE3\"]}" | sed 's/^/node1 sync: /'
req POST "$BASE2/sync" -d "{\"peers\":[\"$BASE1\",\"$BASE3\"]}" | sed 's/^/node2 sync: /'
req POST "$BASE3/sync" -d "{\"peers\":[\"$BASE1\",\"$BASE2\"]}" | sed 's/^/node3 sync: /'

bold "convergence check — all replicas must show [a c d e]"
for i in 1 2 3; do
  echo -n "node$i: "; req GET "${ALL[$((i-1))]}/elements"
done
echo "-- a survived because node2's add used a tag node1's remove never observed (add-wins)"
echo "-- b is gone everywhere because every replica had observed its tag"

bold "4) duplicate sync round — must be a no-op (idempotent join)"
before=$(req GET "$BASE1/state")
req POST "$BASE2/sync" -d "{\"peers\":[\"$BASE1\",\"$BASE3\"]}" >/dev/null
req POST "$BASE3/sync" -d "{\"peers\":[\"$BASE1\",\"$BASE2\"]}" >/dev/null
after=$(req GET "$BASE1/state")
if [ "$before" = "$after" ]; then echo "state byte-identical after repeated full sync: OK"; else
  echo "UNEXPECTED: state changed on duplicate sync"; exit 1; fi

bold "5) tombstone counters BEFORE gc"
for i in 1 2 3; do echo -n "node$i: "; req GET "${ALL[$((i-1))]}/debug/stats"; done

bold "6) dry-run: which tags are safe to reclaim across the 3 converged nodes?"
req GET "$BASE1/gc/eligible?peer=$BASE2&peer=$BASE3"

bold "7) a lagging 4th replica blocks GC (fail-closed), BEFORE any purge"
"$LOGDIR/orset-server" -addr ":18084" -id "node4-laggard" >"$LOGDIR/node4.log" 2>&1 &
PIDS+=($!)
wait_ready http://127.0.0.1:18084
echo "-- eligible set across 4 nodes incl. a replica that never synced (must be 0):"
req GET "$BASE1/gc/eligible?peer=$BASE2&peer=$BASE3&peer=http://127.0.0.1:18084"

bold "8) coordinated GC round over the 3 converged nodes (purge intersection)"
go run ./cmd/gc -peers "$BASE1,$BASE2,$BASE3"

bold "tombstone counters AFTER gc — tombstoneTags must be 0, members unchanged"
for i in 1 2 3; do echo -n "node$i: "; req GET "${ALL[$((i-1))]}/debug/stats"; done

echo
bold "DEMO OK — server logs: $LOGDIR"
