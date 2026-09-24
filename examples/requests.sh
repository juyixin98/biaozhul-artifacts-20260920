#!/usr/bin/env bash
# examples/requests.sh — copy/paste HTTP request samples for the HLC service.
# Assumes a server on http://localhost:8080 (the default listen address).
#
# Run one first:
#   go run . --addr=:8080 --node=node-1
set -u
BASE="${HLC_BASE:-http://localhost:8080}"

# --- Health & status -------------------------------------------------------

curl -s "$BASE/healthz"; echo
curl -s "$BASE/v1/status" | python3 -m json.tool

# --- Local event: mint a timestamp ----------------------------------------

curl -s -X POST "$BASE/v1/tick"; echo

# Peek at the last issued timestamp WITHOUT advancing the clock:
curl -s "$BASE/v1/now"; echo

# --- Receive a remote timestamp (message-receive / send event) -------------

# Envelope form:
curl -s -X POST "$BASE/v1/receive" -H 'Content-Type: application/json' -d '{
  "timestamp": {"physical_ms": 1700000000000, "logical": 7, "node_id": "node-2"}
}'; echo

# Bare timestamp object:
curl -s -X POST "$BASE/v1/receive" -H 'Content-Type: application/json' -d '
  {"physical_ms": 1700000000001, "logical": 3, "node_id": "node-2"}'; echo

# Canonical text wire form as a JSON string:
curl -s -X POST "$BASE/v1/receive" -H 'Content-Type: application/json' \
  -d '"hlc://node-2/1700000000002:3"'; echo

# Canonical text wire form as the raw body:
curl -s -X POST "$BASE/v1/receive" \
  --data-binary 'hlc://node-2/1700000000003:3'; echo

# IEEE-754-safe transport: quote the integers (for browsers / doubles):
curl -s -X POST "$BASE/v1/receive" -H 'Content-Type: application/json' -d '{
  "physical_ms": "1700000000004",
  "logical": "4294967295",
  "node_id": "node-2"
}'; echo

# --- Error cases ------------------------------------------------------------

# Future drift beyond --drift (default 1000ms) -> HTTP 422 future_drift:
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/v1/receive" \
  -H 'Content-Type: application/json' \
  -d '{"timestamp":{"physical_ms": 9999999999999, "logical": 0, "node_id": "x"}}'

# Malformed timestamp -> HTTP 400 invalid_timestamp:
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/v1/receive" \
  -H 'Content-Type: application/json' \
  -d '{"physical_ms": -1, "logical": 0}'

# Counter saturated and physical clock cannot catch up -> HTTP 503 overflow:
# (start with --max-logical=2 and send a remote logical pinned at 2)
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/v1/receive" \
  -H 'Content-Type: application/json' \
  -d '{"timestamp":{"physical_ms": 9000000000000, "logical": 2, "node_id": "x"}}'

# --- A causal exchange between two nodes (two terminals) -------------------
# Terminal A (node-1 :8080) and B (node-2 :8081):
#   A:  go run . --addr=:8080 --node=node-1
#   B:  go run . --addr=:8081 --node=node-2
#
# 1) A mints a send timestamp:
#      SEND=$(curl -s -X POST http://localhost:8080/v1/tick)
# 2) deliver it to B inside the "timestamp" envelope:
#      curl -s -X POST http://localhost:8081/v1/receive \
#        -H 'Content-Type: application/json' \
#        -d "{\"timestamp\":$(echo "$SEND" |
#             python3 -c 'import sys,json;print(json.dumps(json.load(sys.stdin)["timestamp"]))')}"
