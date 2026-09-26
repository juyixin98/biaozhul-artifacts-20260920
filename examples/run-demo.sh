#!/usr/bin/env bash
# run-demo.sh — build, start the server, drive all four scenarios with the
# fault-injection client, and record structured JSON results under results/.
#
# Each scenario needs a FRESH server process (every scenario ends in shutdown),
# so this script starts and stops the server four times on localhost ports.
set -euo pipefail

cd "$(dirname "$0")/.."

PUBLIC="${PUBLIC:-127.0.0.1:28080}"
ADMIN="${ADMIN:-127.0.0.1:28081}"
DRAIN="${DRAIN:-3s}"
CANCEL="${CANCEL:-2s}"
REJECT="${REJECT:-400ms}"
mkdir -p bin results

go build -o bin/server ./cmd/server
go build -o bin/client ./cmd/client

run_scenario() {
  local name="$1"
  echo "=== scenario: ${name} ==="
  ./bin/server -public "$PUBLIC" -admin "$ADMIN" \
    -drain "$DRAIN" -cancel "$CANCEL" -reject-window "$REJECT" \
    > "results/server-${name}.log" 2>&1 &
  local pid=$!
  # Wait for readiness.
  for _ in $(seq 1 50); do
    if curl -fsS "http://${ADMIN}/readyz" >/dev/null 2>&1; then break; fi
    sleep 0.1
  done
  ./bin/client -base "http://${PUBLIC}" -admin "http://${ADMIN}" \
    -scenario "$name" -out "results/client-${name}.json" \
    | tee "results/client-${name}.stdout"
  wait "$pid" || true
  echo
}

for s in drain cancel repeat reject; do
  run_scenario "$s"
done

echo "All scenarios done. Artifacts in results/."
