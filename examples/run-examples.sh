#!/usr/bin/env bash
# End-to-end example walk-through against a running server.
# Usage: ./examples/run-examples.sh [baseUrl]
set -euo pipefail
BASE="${1:-http://localhost:8080}"

say() { printf '\n### %s\n' "$1"; }

say '1) health (empty service)'
curl -s "$BASE/health"; echo

say '2) load offline dataset'
curl -s -X POST "$BASE/load" -H 'Content-Type: application/json' \
  --data @examples/load.json; echo

say '3) query: city=Beijing AND (color=red OR active=yes) AND NOT(color=blue)'
curl -s -X POST "$BASE/query?limit=5" -H 'Content-Type: application/json' \
  --data @examples/query.json; echo

say '4) NOT only complements within the LIVE universe: delete rows 0 and 1'
curl -s -X POST "$BASE/delete" -H 'Content-Type: application/json' \
  --data @examples/delete.json; echo

say '5) NOT(city=Beijing) after deletion — rows 0/1 must NOT come back'
curl -s -X POST "$BASE/query" -H 'Content-Type: application/json' \
  --data @examples/not-query.json; echo

say '6) ids are stable: Beijing survivors are still ids 8 (row 1 was deleted)'
curl -s -X POST "$BASE/query" -H 'Content-Type: application/json' \
  --data '{"op":"eq","column":"city","value":"Beijing"}'; echo

say '7) delete by expression (every red row)'
curl -s -X POST "$BASE/delete" -H 'Content-Type: application/json' \
  --data '{"expr":{"op":"eq","column":"color","value":"red"}}'; echo

say '8) restore rows 0 and 1'
curl -s -X POST "$BASE/restore" -H 'Content-Type: application/json' \
  --data '{"rowIds":[0,1]}'; echo

say '9) index space statistics'
curl -s "$BASE/stats"; echo

say '10) fetch a row by its stable id'
curl -s "$BASE/row/0"; echo
