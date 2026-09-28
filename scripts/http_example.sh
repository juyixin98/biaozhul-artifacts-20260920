#!/usr/bin/env bash
# Example HTTP calls. Start the server first in another terminal:
#   ER_DB_PATH=data/demo.sqlite3 uvicorn entity_resolution.api:app --port 8000
set -euo pipefail
BASE=${ER_BASE:-http://127.0.0.1:8000}

echo "== health =="
curl -s "$BASE/health"; echo

echo "== load corpus =="
curl -s -X POST "$BASE/corpus" -H 'content-type: application/json' \
  --data @fixtures/corpus.example.json; echo

echo "== resolve =="
curl -s -X POST "$BASE/resolve?threshold=0.45"; echo

echo "== add a must-link constraint =="
curl -s -X POST "$BASE/links" -H 'content-type: application/json' \
  -d '{"left":"r-010","right":"r-011","kind":"must"}'; echo

echo "== attempt a contradictory cannot-link (expect STATE_CONFLICT 409) =="
curl -s -X POST "$BASE/links" -H 'content-type: application/json' \
  -d '{"left":"r-010","right":"r-011","kind":"cannot"}'; echo

echo "== lock the confirmed cluster =="
curl -s -X POST "$BASE/clusters/lock" -H 'content-type: application/json' \
  -d '{"cluster_id":"human-gazprom","members":["r-010","r-011"]}'; echo

echo "== affected entities =="
curl -s "$BASE/affected"; echo
