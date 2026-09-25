#!/usr/bin/env bash
# End-to-end acceptance walkthrough against a running server using curl.
# Requires: build completed (target/classes) and a free port (default 18080).
set -euo pipefail
cd "$(dirname "$0")/.."

PORT="${1:-18080}"
BASE="http://127.0.0.1:${PORT}"

say() { printf '\n========== %s ==========\n' "$1"; }

say "health"
curl -s "${BASE}/health"; echo

say "reset"
curl -s -X POST "${BASE}/v1/admin/reset" -d '{}'; echo

say "batch: retract-before-add + out-of-order v3 correction + duplicate replay"
curl -s -X POST "${BASE}/v1/events/batch" \
  -H 'Content-Type: application/json' \
  --data @examples/01-batch-retract-correct-outoforder.json; echo

say "aggregates with ledger verification (?verify=1)"
curl -s "${BASE}/v1/aggregates?verify=1"; echo

say "live events + buffered pending"
curl -s "${BASE}/v1/events"; echo

say "single-op retract-before-add STEP 1 (retract buffered)"
curl -s -X POST "${BASE}/v1/events/ingest" -H 'Content-Type: application/json' \
  --data @examples/02-retract-before-add-step1.json; echo

say "single-op retract-before-add STEP 2 (add releases the buffered retract)"
curl -s -X POST "${BASE}/v1/events/ingest" -H 'Content-Type: application/json' \
  --data @examples/02-retract-before-add-step2.json; echo

say "correction moving order-300 from books to movies (v4)"
curl -s -X POST "${BASE}/v1/events/ingest" -H 'Content-Type: application/json' \
  --data @examples/03-correct-key-change.json; echo

say "correct after terminal RETRACT (order-100 v3) -> CONFLICT"
curl -s -X POST "${BASE}/v1/events/ingest" -H 'Content-Type: application/json' \
  --data @examples/06-correct-after-retract-conflict.json; echo

say "duplicate replay of already applied evt-1 -> DUPLICATE"
curl -s -X POST "${BASE}/v1/events/ingest" -H 'Content-Type: application/json' \
  --data @examples/05-duplicate-replay.json; echo

say "final aggregates (streaming state)"
curl -s "${BASE}/v1/aggregates?verify=1"; echo

say "resolved event ledger"
curl -s "${BASE}/v1/ledger"; echo

say "reference replay of a fresh raw-op stream (what-if)"
curl -s -X POST "${BASE}/v1/replay" -H 'Content-Type: application/json' \
  --data @examples/04-replay-ops.json; echo

say "reference recomputation of the current ledger (empty body)"
curl -s -X POST "${BASE}/v1/replay" -H 'Content-Type: application/json' -d '{}'; echo

say "emit one output snapshot"
curl -s -X POST "${BASE}/v1/emit" -d '{}'; echo

say "invalid request (ADD without value) -> 400"
curl -s -o /dev/null -w "HTTP %{http_code}\n" -X POST "${BASE}/v1/events/ingest" \
  -H 'Content-Type: application/json' \
  -d '{"eventId":"x","op":"ADD","key":"k"}'
