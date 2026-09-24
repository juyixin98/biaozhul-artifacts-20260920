#!/usr/bin/env bash
# Acceptance walk-through for the behavior-tree backend, driven entirely over
# HTTP with curl. Requires a running server:
#
#   BT_DATABASE_URL=postgres://bt065b:bt065b@localhost:5432/btree065b?sslmode=disable \
#     go run ./cmd/bt
#
# Override the base URL with BT_BASE_URL (default http://localhost:8080).
set -euo pipefail

BASE="${BT_BASE_URL:-http://localhost:8080}"
CURL=(curl -sS -H 'Content-Type: application/json')

say() { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
req() { # METHOD PATH [JSON BODY]
  local method="$1" path="$2" body="${3:-}"
  if [[ -n "$body" ]]; then
    "${CURL[@]}" -X "$method" "$BASE$path" -d "$body"
  else
    "${CURL[@]}" -X "$method" "$BASE$path"
  fi
  echo
}

say "health"
req GET /healthz

say "publish tree (Sequence: non-idempotent charge -> gate -> timeout/gate)"
PUBLISH=$(req POST /v1/trees "$(cat "$(dirname "$0")/tree_order.json")")
echo "$PUBLISH"
TREE_ID=$(echo "$PUBLISH" | sed -n 's/.*"tree_id": *"\([^"]*\)".*/\1/p')
[[ -n "$TREE_ID" ]] || { echo "could not read tree_id" >&2; exit 1; }

say "start execution bound to published version"
START=$(req POST /v1/executions "{\"tree_id\":\"$TREE_ID\"}")
echo "$START"
EXEC=$(echo "$START" | sed -n 's/.*"execution_id": *"\([^"]*\)".*/\1/p')

say "tick 1: charge succeeds (physical attempt #1), fast-courier runs"
req POST "/v1/executions/$EXEC/ticks"

say "physical invocation count for non-idempotent charge-card (want 1)"
req GET "/v1/executions/$EXEC/invocations/charge-card"

say "tick 2: charge-card is latched success and MUST NOT run again (still 1)"
req POST "/v1/executions/$EXEC/ticks"
req GET "/v1/executions/$EXEC/invocations/charge-card"

say "fast-courier fails -> Fallback moves to slow-courier (success); receipt gate runs under timeout"
req POST /internal/gates/courier-fast/resolve '{"status":"failure","reason":"courier unavailable"}'
sleep 0.2
req POST "/v1/executions/$EXEC/ticks"

say "let the receipt timeout (2s) elapse without resolving -> timeout fails"
sleep 2.2
req POST "/v1/executions/$EXEC/ticks"

say "late resolution of the timed-out receipt gate is rejected (404)"
req POST /internal/gates/receipt/resolve '{"status":"success"}' || true

say "final execution snapshot (tree failure, charge still exactly once)"
req GET "/v1/executions/$EXEC"
req GET "/v1/executions/$EXEC/invocations/charge-card"

say "DONE"
