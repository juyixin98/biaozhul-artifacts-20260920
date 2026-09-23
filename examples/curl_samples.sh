#!/usr/bin/env bash
# Request samples for the regex-automata JSON service.
# Start the service first:
#   PYTHONPATH=. python3 -m regex_automata serve --host 127.0.0.1 --port 8080
set -euo pipefail
BASE="${BASE:-http://127.0.0.1:8080}"

echo "## GET /health"
curl -s "$BASE/health"; echo

echo "## POST /match  (fullmatch)"
curl -s -X POST "$BASE/match" \
  -H 'Content-Type: application/json' \
  --data @examples/match_request.json; echo

echo "## POST /match  (search, default mode)"
curl -s -X POST "$BASE/match" \
  -H 'Content-Type: application/json' \
  -d '{"pattern":"\\bfoo\\b","text":"a foo bar"}'; echo

echo "## POST /match  (Unicode, code-point semantics)"
curl -s -X POST "$BASE/match" \
  -H 'Content-Type: application/json' \
  -d '{"pattern":"😀+","text":"x😀😀y","mode":"search"}'; echo

echo "## POST /compile (AST + NFA dump)"
curl -s -X POST "$BASE/compile" \
  -H 'Content-Type: application/json' \
  --data @examples/compile_request.json; echo

echo "## POST /compile (positioned error)"
curl -s -X POST "$BASE/compile" \
  -H 'Content-Type: application/json' \
  -d '{"pattern":"a{2,1}"}'; echo
