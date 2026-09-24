#!/usr/bin/env bash
# End-to-end acceptance: builds, runs the server against a throwaway SQLite
# database, drives every required scenario over REAL HTTP with REAL HMAC
# signatures, and asserts on the JSON responses.
set -euo pipefail

cd "$(dirname "$0")/.."

BASE_URL="${BASE_URL:-http://localhost:18099}"
DB="$(mktemp -d)/accept.db"
SECRET="acceptance-secret"
ADMIN="acceptance-admin"

echo "== go vet / test =="
go vet ./...
go test ./...

echo "== build =="
go build -o /tmp/sensorhealth-server ./cmd/sensorhealth
go build -o /tmp/sensorhealth-client ./cmd/ingest-client

echo "== start server =="
HMAC_SECRET="$SECRET" ADMIN_TOKEN="$ADMIN" DB_PATH="$DB" ADDR=":18099" \
  /tmp/sensorhealth-server -seed >/tmp/sh-server.log 2>&1 &
SRV_PID=$!
trap 'kill $SRV_PID 2>/dev/null || true' EXIT

# Wait for readiness.
for i in $(seq 1 50); do
  if curl -fsS "$BASE_URL/healthz" >/dev/null 2>&1; then break; fi
  sleep 0.1
done

post_admin() { # method path json
  curl -fsS -X "$1" "$BASE_URL$2" \
    -H "Authorization: Bearer $ADMIN" -H 'Content-Type: application/json' \
    -d "$3"
}

jget() { python3 -c 'import sys,json;d=json.load(sys.stdin);print(eval(sys.argv[1]))' "$1"; }

echo
echo "== 1) stationary contact device is NOT frozen (per-type thresholds) =="
health=$(curl -fsS "$BASE_URL/api/v1/devices/contact-1/health")
echo "$health" | jget '"healthy=%s epoch=%s cfg=%s" % (d["healthy"], d["epoch"], d["config_version"])'

# Tighten the temp type so the frozen scenario triggers quickly.
post_admin PUT /admin/device-types/temp/config '{
  "stale_enter_timeout":"90s","stale_recover_timeout":"20s",
  "frozen_enter_count":6,"frozen_enter_min_duration":"1ms","frozen_recover_count":2,
  "missing_enter_count":3,"missing_recover_count":1,"backfill_lookback":1000
}' >/dev/null

echo
echo "== 2) healthy batch (samples + heartbeat) =="
/tmp/sensorhealth-client -url "$BASE_URL" -secret "$SECRET" -file examples/batch_healthy.json

echo
echo "== 3) frozen detection: constant value, advancing seq =="
/tmp/sensorhealth-client -url "$BASE_URL" -secret "$SECRET" -file examples/batch_frozen.json
open_frozen=$(curl -fsS "$BASE_URL/api/v1/alerts?device_id=temp-1&status=open&kind=frozen")
echo "$open_frozen" | jget '"open frozen alerts: %d, trigger=%s..%s config_version=%s" % (len(d["alerts"]), d["alerts"][0]["trigger_range"]["seq_start"], d["alerts"][0]["trigger_range"]["seq_end"], d["alerts"][0]["config_version"])'

echo
echo "== 4) gap opens on a small sequence jump (same sequence line) =="
/tmp/sensorhealth-client -url "$BASE_URL" -secret "$SECRET" -file examples/batch_smallgap.json
curl -fsS "$BASE_URL/api/v1/alerts?device_id=temp-1&status=open&kind=gap" \
  | jget '"open gap alerts: %d, trigger=%s..%s" % (len(d["alerts"]), d["alerts"][0]["trigger_range"]["seq_start"], d["alerts"][0]["trigger_range"]["seq_end"])'

echo
echo "== 5) batch backfill (out of order) recovers the gap =="
/tmp/sensorhealth-client -url "$BASE_URL" -secret "$SECRET" -file examples/batch_smallbackfill.json
remaining=$(curl -fsS "$BASE_URL/api/v1/alerts?device_id=temp-1&status=open&kind=gap")
echo "$remaining" | jget '"remaining open gap alerts after backfill: %d" % len(d["alerts"])'
curl -fsS "$BASE_URL/api/v1/alerts?device_id=temp-1&status=recovered&kind=gap" \
  | jget '"recovered gap alerts (with recovery range): %d" % len(d["alerts"])'

echo
echo "== 5b) a huge forward jump opens a NEW epoch (not a giant fake gap) =="
epoch_before=$(curl -fsS "$BASE_URL/api/v1/devices/temp-1/health" | jget 'd["epoch"]')
/tmp/sensorhealth-client -url "$BASE_URL" -secret "$SECRET" -file examples/batch_rebase.json | tail -1
epoch_after=$(curl -fsS "$BASE_URL/api/v1/devices/temp-1/health" | jget 'd["epoch"]')
echo "epoch before=$epoch_before after=$epoch_after (expect after > before)"
[ "$epoch_after" -gt "$epoch_before" ]
# No gap alert should be raised for the rebase.
curl -fsS "$BASE_URL/api/v1/alerts?device_id=temp-1&status=open&kind=gap" \
  | jget '"open gap alerts after rebase: %d (expect 0)" % len(d["alerts"])'

echo
echo "== 6) unsigned request is rejected =="
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE_URL/api/v1/ingest" \
  -H 'Content-Type: application/json' -d '{"device_id":"temp-1","messages":[]}')
echo "unsigned POST -> HTTP $code (expect 401)"
[ "$code" = "401" ]

echo
echo "== 7) stale opens after silence (use a fresh short-timeout device) =="
post_admin POST /admin/devices '{"id":"stale-1","type":"short"}' >/dev/null
post_admin PUT /admin/device-types/short/config '{
  "stale_enter_timeout":"2s","stale_recover_timeout":"1s",
  "frozen_enter_count":50,"frozen_enter_min_duration":"1m","frozen_recover_count":2,
  "missing_enter_count":5,"missing_recover_count":2,"backfill_lookback":100
}' >/dev/null
/tmp/sensorhealth-client -url "$BASE_URL" -secret "$SECRET" -device stale-1 -heartbeat >/dev/null
sleep 3
curl -fsS -X POST "$BASE_URL/admin/sweep" -H "Authorization: Bearer $ADMIN" >/dev/null
curl -fsS "$BASE_URL/api/v1/alerts?device_id=stale-1&status=open&kind=stale" \
  | jget '"open stale alerts after silence: %d" % len(d["alerts"])'

echo
echo "== 8) persistence: restart the server, open alerts survive =="
kill $SRV_PID; wait $SRV_PID 2>/dev/null || true
HMAC_SECRET="$SECRET" ADMIN_TOKEN="$ADMIN" DB_PATH="$DB" ADDR=":18099" \
  /tmp/sensorhealth-server >>/tmp/sh-server.log 2>&1 &
SRV_PID=$!
for i in $(seq 1 50); do
  if curl -fsS "$BASE_URL/healthz" >/dev/null 2>&1; then break; fi
  sleep 0.1
done
curl -fsS "$BASE_URL/api/v1/alerts?device_id=stale-1&status=open&kind=stale" \
  | jget '"after restart, open stale alerts still present: %d" % len(d["alerts"])'

echo
echo "ALL ACCEPTANCE CHECKS PASSED"
