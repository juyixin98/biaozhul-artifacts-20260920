#!/usr/bin/env bash
# End-to-end acceptance: starts the gateway against PostgreSQL, streams
# the sample inputs through both versioned clients, exercises the mapping
# hot-swap, and verifies the audit trail. Fails loudly on any mismatch.
set -euo pipefail
cd "$(dirname "$0")/.."

ADDR=127.0.0.1:50051
DSN="${GATEWAY_DATABASE_DSN:-postgres://telegw:telegw@localhost:5432/telegw}"
export GATEWAY_DATABASE_DSN="$DSN"

echo "== 1. start gateway (mapping strict-v1) =="
./bin/gateway -addr "$ADDR" -mapping strict-v1 >/tmp/telegw-acceptance.log 2>&1 &
GW=$!
trap 'kill $GW 2>/dev/null || true' EXIT
sleep 1.5

echo "== 2. v1 client -> v2 output =="
out1=$(./bin/clientv1 -addr "$ADDR" -input examples/readings_v1.jsonl)
echo "$out1" | head -2
echo "$out1" | grep -q '"temperatureMillikelvin":"2753000"' || { echo "FAIL: unit transform"; exit 1; }
echo "$out1" | grep -q '"temperatureMillikelvin":"2731500"' || { echo "FAIL: explicit zero"; exit 1; }
echo "$out1" | grep -q '"sourceVersion":"telemetry/v1"' || { echo "FAIL: provenance"; exit 1; }

echo "== 3. v2 client under strict mapping -> locatable errors =="
out2=$(./bin/clientv2 -addr "$ADDR" -input examples/readings_v2.jsonl)
echo "$out2" | grep -q '"fieldPath":"condition"' || { echo "FAIL: locatable enum error"; exit 1; }
echo "$out2" | grep -q 'MAINTENANCE(4)' || { echo "FAIL: raw enum value"; exit 1; }

echo "== 4. hot-swap mapping to carry-v1 =="
./bin/gwctl -addr "$ADDR" reload carry-v1 | grep -q 'active mapping: carry-v1' || { echo "FAIL: hot-swap"; exit 1; }
out3=$(./bin/clientv2 -addr "$ADDR" -input examples/readings_v2.jsonl)
echo "$out3" | grep -q '"unmapped_condition_raw":"4"' || { echo "FAIL: carried raw enum"; exit 1; }
echo "$out3" | grep -q '"mappingVersion":"carry-v1"' || { echo "FAIL: mapping version in meta"; exit 1; }

echo "== 5. stats and audit trail =="
./bin/gwctl -addr "$ADDR" stats
rows=$(psql "$DSN" -tAc "SELECT count(*) FROM conversion_audit")
echo "audit rows: $rows"
[ "$rows" -ge 12 ] || { echo "FAIL: audit trail incomplete"; exit 1; }
errs=$(psql "$DSN" -tAc "SELECT count(*) FROM conversion_audit WHERE status='error' AND error_detail->>'field_path'='condition'")
[ "$errs" -ge 2 ] || { echo "FAIL: error audit rows"; exit 1; }

kill $GW
trap - EXIT
echo "ACCEPTANCE OK"
