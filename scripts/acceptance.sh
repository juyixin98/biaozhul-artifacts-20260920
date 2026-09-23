#!/usr/bin/env bash
# acceptance.sh — end-to-end acceptance for the account-state snapshot
# service. Builds, seeds blocks, builds snapshots, prunes, exercises lease
# protection/expiry, and runs the independent replay cross-check.
#
# Usage: ./scripts/acceptance.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

ADDR="127.0.0.1:18095"
BASE="http://$ADDR"
WORK="$(mktemp -d)"
DATA="$WORK/data"
trap 'kill ${SRV_PID:-} 2>/dev/null || true; rm -rf "$WORK"' EXIT

echo "== go build =="
go build -o "$WORK/snapd" ./cmd/snapd
go build -o "$WORK/snapdemo" ./cmd/snapdemo

echo "== unit + race tests =="
go test -race -count=1 ./...

echo "== generate deterministic genesis + 12 signed proposals =="
"$WORK/snapdemo" demoinit "$WORK/genesis.json" >/dev/null
"$WORK/snapdemo" genblocks "$WORK/proposals.jsonl" 12 >/dev/null

echo "== start snapd (lease TTL 2s) =="
"$WORK/snapd" -addr "$ADDR" -data "$DATA" -genesis "$WORK/genesis.json" \
  -lease-ttl 2s &>"$WORK/srv.log" &
SRV_PID=$!

# Wait for health.
for _ in $(seq 1 50); do
  curl -sf "$BASE/healthz" >/dev/null 2>&1 && break
  sleep 0.1
done

echo "== append 12 blocks (real Ed25519 signatures) =="
"$WORK/snapdemo" replay "$BASE" "$WORK/proposals.jsonl" | tail -1

echo "== build snapshots at 4,8,12 =="
for h in 4 8 12; do
  curl -sf -X POST "$BASE/v1/snapshots" -d "{\"height\":$h}" \
    | python3 -c 'import sys,json;print("  snapshot",json.load(sys.stdin)["height"])'
done

echo "== history query under an explicit lease =="
LID=$(curl -sf -X POST "$BASE/v1/leases" -d '{"height":10}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
curl -sf "$BASE/v1/state?lease=$LID" \
  | python3 -c 'import sys,json;d=json.load(sys.stdin);print("  h10 via",d["source"],"root",d["view"]["summary"]["state_root"])'

echo "== build extra snapshot@6, prune (keeps newest 3: 12,8,6; deltas 1..5) =="
curl -sf -X POST "$BASE/v1/snapshots" -d '{"height":6}' >/dev/null
curl -sf -X POST "$BASE/v1/prune" \
  | python3 -c 'import sys,json;d=json.load(sys.stdin);print("  deleted snaps",d["snapshots"]["deleted_heights"],"deltas through",d["delta_delete_through"])'

echo "== pruned history must fail explicitly (expect HTTP 410) =="
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/v1/leases" -d '{"height":2}')
[ "$code" = "410" ] && echo "  got 410 history_pruned (correct)" || { echo "  FAIL: $code"; exit 1; }

echo "== expired lease must fail explicitly (expect HTTP 410) =="
EXP=$(curl -sf -X POST "$BASE/v1/leases" -d '{"height":12}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
sleep 2.2
code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/v1/state?lease=$EXP")
[ "$code" = "410" ] && echo "  got 410 lease_expired (correct)" || { echo "  FAIL: $code"; exit 1; }

echo "== independent replay cross-check =="
curl -sf -X POST "$BASE/v1/replay" \
  | python3 -c 'import sys,json;d=json.load(sys.stdin);assert d["matches"],d;print("  MATCH blocks=%d snapshots=%d"%(d["blocks_checked"],d["snapshots_checked"]))'

echo "== restart persistence: new snapd sees same snapshots =="
kill "$SRV_PID"; SRV_PID=""
sleep 0.3
"$WORK/snapd" -addr "$ADDR" -data "$DATA" -lease-ttl 2s &>"$WORK/srv2.log" &
SRV_PID=$!
for _ in $(seq 1 50); do curl -sf "$BASE/healthz" >/dev/null 2>&1 && break; sleep 0.1; done
curl -sf "$BASE/v1/snapshots" \
  | python3 -c 'import sys,json;d=json.load(sys.stdin);print("  snapshots after restart:",[s["height"] for s in d["snapshots"]]);assert len(d["snapshots"])==3'
curl -sf -X POST "$BASE/v1/replay" \
  | python3 -c 'import sys,json;assert json.load(sys.stdin)["matches"];print("  replay after restart: MATCH")'

echo
echo "ALL ACCEPTANCE CHECKS PASSED"
