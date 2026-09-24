#!/usr/bin/env bash
# End-to-end walkthrough against a running server (start it first):
#   go run ./cmd/server -addr 127.0.0.1:8080
#
# Covers: writes in every phase, a partition, duplicated control messages,
# the lost-response fault, cutover fencing, and the final invariant report.
set -euo pipefail
B="${BASE:-http://127.0.0.1:8080}"
j() { python3 -c 'import sys,json; d=json.load(sys.stdin); out={k:d[k] for k in sys.argv[1:] if k in d}; print("   ", json.dumps(out) if out else json.dumps(d))' "$@"; }

echo "0. reset to a clean shard"
curl -s -XPOST "$B/control/reset" >/dev/null

echo "1. pre-snapshot writes on A (epoch 1)"
curl -s -XPOST "$B/append" -d '{"node":"A","epoch":1,"key":"k1","value":"v1"}' | j seq epoch primary
curl -s -XPOST "$B/append" -d '{"node":"A","epoch":1,"key":"k2","value":"v2"}' | j seq epoch primary

echo "2. begin_snapshot (command_id=b) pins the snapshot point"
curl -s -XPOST "$B/migration/begin_snapshot" -d '{"command_id":"b"}' | j phase snapshot_seq

echo "3. writes keep flowing during the snapshot phase"
curl -s -XPOST "$B/append" -d '{"node":"A","epoch":1,"key":"k3","value":"v3"}' | j seq epoch primary

echo "4. partition B -> snapshot install refused (502)"
curl -s -XPOST "$B/nodes/B/link" -d '{"up":false}' >/dev/null
curl -s -o /tmp/err -w "   HTTP %{http_code} " -XPOST "$B/migration/complete_snapshot" -d '{"command_id":"s"}'; cat /tmp/err; echo
curl -s -XPOST "$B/nodes/B/link" -d '{"up":true}' >/dev/null
echo "   reconnect B, retry the SAME command_id=s -> succeeds"
curl -s -XPOST "$B/migration/complete_snapshot" -d '{"command_id":"s"}' | j phase installed b_last_seq

echo "5. catch-up phase: new write, then drain"
curl -s -XPOST "$B/append" -d '{"node":"A","epoch":1,"key":"k4","value":"v4"}' >/dev/null
curl -s -XPOST "$B/migration/catch_up" -d '{"command_id":"c1"}' | j phase installed lag
echo "   duplicate control message c1 -> replay, state does not move twice"
curl -s -XPOST "$B/migration/catch_up" -d '{"command_id":"c1"}' | j replayed lag

echo "6. lost-response fault: committed write, first response lost, retry dedupes"
curl -s -XPOST "$B/nodes/A/drop-append" -d '{"drop":true}' >/dev/null
curl -s -o /tmp/err -w "   first  HTTP %{http_code} " -XPOST "$B/append" -d '{"node":"A","epoch":1,"key":"k5","value":"v5"}'; cat /tmp/err; echo
curl -s -XPOST "$B/append" -d '{"node":"A","epoch":1,"key":"k5","value":"v5"}' | j seq deduplicated primary
curl -s -XPOST "$B/nodes/A/drop-append" -d '{"drop":false}' >/dev/null
echo "   drain k5 to B with a fresh catch-up (lag must reach 0 before cutover)"
curl -s -XPOST "$B/migration/catch_up" -d '{"command_id":"c2"}' | j phase installed lag

echo "7. prepare cutover -> cluster frozen (503 on writes)"
curl -s -XPOST "$B/migration/prepare_cutover" -d '{"command_id":"p"}' | j phase cutover_seq
curl -s -o /dev/null -w "   write while switching -> HTTP %{http_code}\n" -XPOST "$B/append" -d '{"node":"A","epoch":1,"key":"kx","value":"x"}'

echo "8. commit with B partitioned fails and does NOT bump the epoch"
curl -s -XPOST "$B/nodes/B/link" -d '{"up":false}' >/dev/null
curl -s -o /tmp/err -w "   HTTP %{http_code} " -XPOST "$B/migration/commit_cutover" -d '{"command_id":"co"}'; cat /tmp/err; echo
curl -s "$B/status" | j epoch primary phase
curl -s -XPOST "$B/nodes/B/link" -d '{"up":true}' >/dev/null
echo "   retry same command_id=co -> committed (failures are not cached)"
curl -s -XPOST "$B/migration/commit_cutover" -d '{"command_id":"co"}' | j phase epoch primary

echo "9. after switch the old primary is fenced"
curl -s -o /dev/null -w "   A with epoch 1 -> HTTP %{http_code} (stale_epoch)\n" -XPOST "$B/append" -d '{"node":"A","epoch":1,"key":"late","value":"x"}'
curl -s -o /dev/null -w "   A forging epoch 2 -> HTTP %{http_code} (not_primary)\n" -XPOST "$B/append" -d '{"node":"A","epoch":2,"key":"late2","value":"x"}'
curl -s -XPOST "$B/append" -d '{"node":"B","epoch":2,"key":"k6","value":"v6"}' | j seq epoch primary

echo "10. invariant report"
curl -s "$B/verify" | python3 -m json.tool
