#!/usr/bin/env bash
# End-to-end smoke test against a running DAMS server.
# Usage: ADMIN=... COLLECTOR=... ANALYST=... AUDITOR=... BASE=http://localhost:8080 ./examples/smoke.sh
set -euo pipefail

BASE="${BASE:-http://localhost:8080}"
ADMIN="${ADMIN:?set ADMIN api key}"
COLLECTOR="${COLLECTOR:?set COLLECTOR api key}"
ANALYST="${ANALYST:?set ANALYST api key}"
AUDITOR="${AUDITOR:?set AUDITOR api key}"
DIR="$(cd "$(dirname "$0")" && pwd)"

j() { command -v jq >/dev/null && jq "$@" || cat; }

echo "== health =="
curl -fsS "$BASE/healthz"; echo

echo "== configure rules (admin) =="
curl -fsS -X POST "$BASE/v1/rules/" -H "Authorization: Bearer $ADMIN" \
  -H 'Content-Type: application/json' --data @"$DIR/rule_frequency.json" | j .
curl -fsS -X POST "$BASE/v1/rules/" -H "Authorization: Bearer $ADMIN" \
  -H 'Content-Type: application/json' --data @"$DIR/rule_sensitive_hours.json" | j .

echo "== ingest two normal events (collector) =="
curl -fsS -X POST "$BASE/v1/events:batch" -H "Authorization: Bearer $COLLECTOR" \
  -H 'Content-Type: application/json' --data @"$DIR/events_batch.json" | j .

echo "== replay the same batch: inserted=0, duplicates=2 =="
curl -fsS -X POST "$BASE/v1/events:batch" -H "Authorization: Bearer $COLLECTOR" \
  -H 'Content-Type: application/json' --data @"$DIR/events_batch.json" | j .

echo "== masked export (auditor) — sql_text/client_ip are ***MASKED*** =="
curl -fsS "$BASE/v1/events" -H "Authorization: Bearer $AUDITOR" | j .

echo "== alerts list (analyst) =="
curl -fsS "$BASE/v1/alerts/" -H "Authorization: Bearer $ANALYST" | j .

echo "== audit chain verification (auditor) =="
curl -fsS "$BASE/v1/audit/verify" -H "Authorization: Bearer $AUDITOR" | j .
