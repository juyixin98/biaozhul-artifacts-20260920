#!/usr/bin/env bash
# End-to-end walkthrough against a locally running server.
# Usage: ./examples/requests.sh   (server: http://127.0.0.1:8080)
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8080}"

say() { printf '\n=== %s ===\n' "$*"; }

say "health"
curl -sS "$BASE/healthz"; echo

say "ingest 7 samples (two series, samples straddle minute/hour boundaries)"
curl -sS -X POST "$BASE/v1/ingest" \
  -H 'Content-Type: application/json' \
  --data @examples/ingest.json; echo

say "list series"
curl -sS "$BASE/v1/series" | head -c 600; echo

say "query raw layer: 02:00:00-02:01:00 (host=h1)"
curl -sS "$BASE/v1/query?metric=cpu.usage&label=host=h1&label=dc=dc-a&layer=raw&start=7200&end=7260" | head -c 800; echo

say "query minute layer: 02:00-03:00 (empty minutes included as count=0)"
curl -sS "$BASE/v1/query?metric=cpu.usage&label=host=h1&label=dc=dc-a&layer=minute&start=7200&end=10800" | head -c 1200; echo

say "query hour layer: 02:00-05:00 (the 04:00 hour has no data -> explicit count=0 bucket)"
curl -sS "$BASE/v1/query?metric=cpu.usage&label=host=h1&label=dc=dc-a&layer=hour&start=7200&end=18000"; echo

say "query hour layer using RFC3339 timestamps"
curl -sS "$BASE/v1/query?metric=cpu.usage&label=host=h1&label=dc=dc-a&layer=hour&start=1970-01-01T02:00:00Z&end=1970-01-01T04:00:00Z"; echo

say "late arrival: add a sample deep in the past (02:00:30), merged into existing buckets"
curl -sS -X POST "$BASE/v1/ingest" -H 'Content-Type: application/json' \
  -d '{"samples":[{"metric":"cpu.usage","labels":{"host":"h1","dc":"dc-a"},"ts":7230,"value":50.5}]}'; echo
curl -sS "$BASE/v1/query?metric=cpu.usage&label=host=h1&label=dc=dc-a&layer=minute&start=7200&end=7260"; echo

say "historical correction: overwrite the value at 02:00:00, propagated to minute+hour"
curl -sS -X POST "$BASE/v1/correct" \
  -H 'Content-Type: application/json' \
  --data @examples/correct.json; echo
curl -sS "$BASE/v1/query?metric=cpu.usage&label=host=h1&label=dc=dc-a&layer=hour&start=7200&end=10800"; echo

say "correction at a second that never had raw data -> 409 (unrecoverable detail)"
curl -sS -o /dev/null -w 'HTTP %{http_code}\n' -X POST "$BASE/v1/correct" \
  -H 'Content-Type: application/json' \
  -d '{"metric":"cpu.usage","labels":{"host":"h1","dc":"dc-a"},"ts":7201,"value":1}'; echo

say "prune raw samples older than 03:00:00"
curl -sS -X POST "$BASE/v1/admin/prune" -H 'Content-Type: application/json' \
  -d '{"cutoff":10800}'; echo

say "after prune, correcting pruned history -> 409; minute/hour layers still served"
curl -sS -o /dev/null -w 'HTTP %{http_code}\n' -X POST "$BASE/v1/correct" \
  -H 'Content-Type: application/json' \
  -d '{"metric":"cpu.usage","labels":{"host":"h1","dc":"dc-a"},"ts":7200,"value":1}'; echo
curl -sS "$BASE/v1/query?metric=cpu.usage&label=host=h1&label=dc=dc-a&layer=hour&start=7200&end=10800"; echo

say "persist snapshot to disk"
curl -sS -X POST "$BASE/v1/admin/snapshot"; echo
