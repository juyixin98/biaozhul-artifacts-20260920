#!/usr/bin/env bash
# End-to-end curl walkthrough. Start the server first (scripts/run.sh 8080),
# then run: scripts/examples.sh
#
# Demonstrates: create, out-of-order duplicates, watermark boundary, memory
# release, epoch fencing, and a full migration handoff.
set -euo pipefail
BASE="${BASE:-http://localhost:8080}"
P="demo"
RET=10000
j() { jq .; }

echo "### 0. health"
curl -s "$BASE/health" | j

echo "### 1. create partition $P (epoch=1, retention=10000ms)"
curl -s -X PUT "$BASE/partitions/$P" \
  -H 'Content-Type: application/json' \
  -d "{\"epoch\":1,\"retentionMillis\":$RET}" | j

echo "### 2. first delivery of event A at t=1000 -> NEW"
curl -s -X POST "$BASE/partitions/$P/events" -H 'Content-Type: application/json' \
  -d '{"eventId":"A","eventTime":1000,"epoch":1}' | j

echo "### 3. out-of-order redelivery of A at t=900 -> DUPLICATE"
curl -s -X POST "$BASE/partitions/$P/events" -H 'Content-Type: application/json' \
  -d '{"eventId":"A","eventTime":900,"epoch":1}' | j

echo "### 4. stale routing version (epoch=0) -> 409 INVALID_ROUTING_VERSION"
curl -s -i -X POST "$BASE/partitions/$P/events" -H 'Content-Type: application/json' \
  -d '{"eventId":"B","eventTime":2000,"epoch":0}' | sed -n '1,12p'

echo "### 5. advance watermark to 10999 (A anchored 1000 + 10000 = 11000) -> still retained"
curl -s -X POST "$BASE/partitions/$P/watermark" -H 'Content-Type: application/json' \
  -d '{"watermark":10999,"epoch":1}' | j

echo "### 6. advance watermark to exactly 11000 -> boundary inclusive, A released"
curl -s -X POST "$BASE/partitions/$P/watermark" -H 'Content-Type: application/json' \
  -d '{"watermark":11000,"epoch":1}' | j

echo "### 7. A delivered again at t=11000 -> NEW (retention elapsed)"
curl -s -X POST "$BASE/partitions/$P/events" -H 'Content-Type: application/json' \
  -d '{"eventId":"A","eventTime":11000,"epoch":1}' | j

echo "### 8. migration: export snapshot from source (freezes writes)"
SNAP=$(curl -s -X POST "$BASE/partitions/$P/migration/export" \
  -H 'Content-Type: application/json' -d '{"epoch":1}')
echo "$SNAP" | j

echo "### 9. import snapshot into partition demo-new under epoch 2"
curl -s -X POST "$BASE/partitions/$P-new/migration/import" \
  -H 'Content-Type: application/json' \
  -d "$(echo "$SNAP" | jq -c '{snapshot: .snapshot, newEpoch: 2}')" | j

echo "### 10. duplicate A redelivered to the NEW owner -> DUPLICATE (state traveled)"
curl -s -X POST "$BASE/partitions/$P-new/events" -H 'Content-Type: application/json' \
  -d '{"eventId":"A","eventTime":11000,"epoch":2}' | j

echo "### 11. old epoch routed to new owner -> 409"
curl -s -i -X POST "$BASE/partitions/$P-new/events" -H 'Content-Type: application/json' \
  -d '{"eventId":"A","eventTime":11000,"epoch":1}' | sed -n '1,12p'

echo "### 12. complete cutover on the source"
curl -s -X POST "$BASE/partitions/$P/migration/complete" \
  -H 'Content-Type: application/json' -d '{"epoch":1}' | j
