#!/usr/bin/env bash
# End-to-end local demo: Anvil -> deploy -> API -> example calls.
# Usage:  ./scripts/demo.sh        (Ctrl-C or it exits by itself at the end)
set -euo pipefail
cd "$(dirname "$0")/.."

export PATH="$HOME/.foundry/bin:$PATH"
RPC_PORT="${RPC_PORT:-8545}"
API_PORT="${API_PORT:-8000}"
RPC_URL="http://127.0.0.1:${RPC_PORT}"
export RPC_URL

ANVIL_LOG=$(mktemp)
UVICORN_LOG=$(mktemp)
cleanup() {
  [[ -n "${UVICORN_PID:-}" ]] && kill "$UVICORN_PID" 2>/dev/null || true
  [[ -n "${ANVIL_PID:-}" ]] && kill "$ANVIL_PID" 2>/dev/null || true
}
trap cleanup EXIT

echo "==> building contracts"
forge build --quiet

echo "==> exporting ABIs"
.venv/bin/python scripts/export_abis.py

echo "==> starting anvil on :${RPC_PORT} (log: $ANVIL_LOG)"
anvil --port "$RPC_PORT" --silent >"$ANVIL_LOG" 2>&1 &
ANVIL_PID=$!
sleep 1

echo "==> deploying contracts"
.venv/bin/python scripts/deploy_local.py

echo "==> starting API on :${API_PORT} (log: $UVICORN_LOG)"
.venv/bin/uvicorn app.main:app --app-dir backend --port "$API_PORT" >"$UVICORN_LOG" 2>&1 &
UVICORN_PID=$!
for i in $(seq 1 30); do
  curl -sf "http://127.0.0.1:${API_PORT}/health" >/dev/null && break
  sleep 0.3
done

API="http://127.0.0.1:${API_PORT}"
echo; echo "==> GET /health"
curl -s "$API/health"; echo

echo; echo "==> GET /vault (empty vault)"
curl -s "$API/vault"; echo

echo; echo "==> POST /faucet (mint 1000 MOCK to server account)"
curl -s -X POST "$API/faucet" -H 'content-type: application/json' \
  -d '{"amount":"1000000000000000000000"}'; echo

echo; echo "==> GET /vault/preview/deposit?assets=1 (tiny deposit: 1 wei)"
curl -s "$API/vault/preview/deposit?assets=1"; echo

echo; echo "==> POST /vault/deposit 1 wei"
curl -s -X POST "$API/vault/deposit" -H 'content-type: application/json' \
  -d '{"assets":"1"}'; echo

echo; echo "==> POST /vault/deposit 3 tokens with 1% max slippage"
curl -s -X POST "$API/vault/deposit" -H 'content-type: application/json' \
  -d '{"assets":"3000000000000000000","max_slippage_bps":100}'; echo

echo; echo "==> GET /vault (after deposits)"
curl -s "$API/vault"; echo

echo; echo "==> POST /vault/redeem (all shares)"
SHARES=$(curl -s "$API/account/$(curl -s "$API/health" | .venv/bin/python -c 'import sys,json;print(json.load(sys.stdin)["server_account"])')" | .venv/bin/python -c 'import sys,json;print(json.load(sys.stdin)["share_balance"])')
echo "redeeming $SHARES shares"
curl -s -X POST "$API/vault/redeem" -H 'content-type: application/json' \
  -d "{\"shares\":\"$SHARES\"}"; echo

echo; echo "==> GET /vault (drained)"
curl -s "$API/vault"; echo

echo; echo "demo complete."
