#!/usr/bin/env bash
# End-to-end acceptance check for the swap-router.
# Builds the binary, starts it on a temp SQLite DB, seeds the example
# snapshot and exercises direct quotes, multi-hop routing with explicit
# costs, tie handling and every documented rejection path.
set -euo pipefail

cd "$(dirname "$0")/.."

BIN="target/debug/swap-router"
DB="$(mktemp -t swap-router-acceptance.XXXXXX).db"
# Pick a free ephemeral port (the environment has fixed services on many
# common ports), and bind the server to 127.0.0.1 only.
pick_port() {
  python3 - <<'PY'
import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
PY
}
PORT="$(pick_port)"
ADDR="127.0.0.1:${PORT}"
BASE="http://${ADDR}"

echo "==> building"
cargo build --offline

cleanup() {
  if [[ -n "${SERVER_PID:-}" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
  rm -f "$DB" "$DB-wal" "$DB-shm"
}
trap cleanup EXIT

echo "==> starting server on $ADDR (db=$DB)"
SWAP_ROUTER_DB="$DB" SWAP_ROUTER_ADDR="$ADDR" "$BIN" serve >/tmp/swap-router-acceptance.log 2>&1 &
SERVER_PID=$!

ready=0
for _ in $(seq 1 50); do
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "server exited early; log:"; cat /tmp/swap-router-acceptance.log; exit 1
  fi
  if out=$(curl -sf "$BASE/healthz" 2>/dev/null) && [[ "$out" == *'"ok"'* ]]; then
    ready=1; break
  fi
  sleep 0.1
done
[[ "$ready" == 1 ]] || { echo "server did not become ready"; cat /tmp/swap-router-acceptance.log; exit 1; }

echo "==> seeding example snapshot"
SEED=$(curl -sf -X POST "$BASE/snapshots" \
  -H 'content-type: application/json' \
  --data @examples/snapshot.json)
echo "$SEED"
SID=$(printf '%s' "$SEED" | sed -n 's/.*"snapshot_id":\([0-9]*\).*/\1/p')
[[ "$SID" =~ ^[0-9]+$ ]]

echo "==> 1. direct quote USDC -> WETH (must choose direct pool, show per-hop evidence)"
Q1=$(curl -sf -X POST "$BASE/quotes" -H 'content-type: application/json' \
  --data @examples/quote_direct.json)
echo "$Q1" | python3 -m json.tool
echo "$Q1" | grep -q '"snapshot_id":'"$SID"
echo "$Q1" | grep -q '"content_hash"'
echo "$Q1" | grep -q '"pool-usdc-weth"'
echo "$Q1" | python3 -c '
import json, sys
r = json.load(sys.stdin)
assert len(r["hops"]) == 1, r
h = r["hops"][0]
# gross and net must be equal with zero cost, both positive integers
assert int(h["gross_amount_out"]) == int(h["net_amount_out"]) > 0
assert int(h["net_amount_out"]) == int(r["net_amount_out"])
assert int(r["min_output"]) <= int(r["net_amount_out"])
assert int(h["amount_in"]) == 1_000_000_000
# evidence sanity: output computed against the quoted reserves
ai, ri, ro = int(h["amount_in"]), int(h["reserve_in"]), int(h["reserve_out"])
fee = h["fee_bps"]
expect = (ai * (10000-fee) * ro) // (ri*10000 + ai*(10000-fee))
assert int(h["gross_amount_out"]) == expect, (int(h["gross_amount_out"]), expect)
print("hop math verified")
'

echo "==> 2. multi-hop quote USDC -> WBTC with per-hop explicit cost"
Q2=$(curl -sf -X POST "$BASE/quotes" -H 'content-type: application/json' \
  --data @examples/quote_multihop.json)
echo "$Q2" | python3 -m json.tool
echo "$Q2" | python3 -c '
import json, sys
r = json.load(sys.stdin)
assert 1 <= len(r["hops"]) <= 3
for i, h in enumerate(r["hops"]):
    assert int(h["explicit_cost"]) == 100
    assert int(h["net_amount_out"]) == int(h["gross_amount_out"]) - 100
    if i > 0:
        assert int(h["amount_in"]) == int(r["hops"][i-1]["net_amount_out"]), "carry rule"
assert int(r["net_amount_out"]) > 0
assert int(r["min_output"]) < int(r["net_amount_out"])  # 1% slippage
print("multi-hop carry + cost verified, chosen:", [h["pool_id"] for h in r["hops"]])
'

echo "==> 3. client min_output too high -> 422 OUTPUT_BELOW_MINIMUM"
code=$(curl -s -o /tmp/swap-router-err.json -w '%{http_code}' -X POST "$BASE/quotes" \
  -H 'content-type: application/json' --data @examples/quote_below_min.json)
echo "http $code: $(cat /tmp/swap-router-err.json)"
[[ "$code" == "422" ]]
grep -q OUTPUT_BELOW_MINIMUM /tmp/swap-router-err.json

echo "==> 4. unknown token precision at ingestion -> 400"
code=$(curl -s -o /tmp/swap-router-err.json -w '%{http_code}' -X POST "$BASE/snapshots" \
  -H 'content-type: application/json' -d '{
    "assets": [{"id":"A","decimals":18}],
    "pools": [{"id":"p","token0":"A","token1":"GHOST","reserve0":"1000","reserve1":"1000","fee_bps":30}]}')
echo "http $code: $(cat /tmp/swap-router-err.json)"
[[ "$code" == "400" ]]
grep -q "precision is unknown" /tmp/swap-router-err.json

echo "==> 5. zero reserve at ingestion -> 400"
code=$(curl -s -o /tmp/swap-router-err.json -w '%{http_code}' -X POST "$BASE/snapshots" \
  -H 'content-type: application/json' -d '{
    "assets": [{"id":"A","decimals":18},{"id":"B","decimals":6}],
    "pools": [{"id":"p","token0":"A","token1":"B","reserve0":"0","reserve1":"1000","fee_bps":30}]}')
echo "http $code: $(cat /tmp/swap-router-err.json)"
[[ "$code" == "400" ]]
grep -qi "zero reserve" /tmp/swap-router-err.json

echo "==> 6. dust input -> 422 NO_FEASIBLE_PATH (tiny pool, floor to zero)"
curl -sf -X POST "$BASE/snapshots" -H 'content-type: application/json' -d '{
  "assets":[{"id":"AA","decimals":18},{"id":"BB","decimals":18}],
  "pools":[{"id":"p1","token0":"AA","token1":"BB","reserve0":"1000","reserve1":"1000","fee_bps":30}]}' >/dev/null
DUST_SID=$(curl -sf "$BASE/snapshots/latest" | sed -n 's/.*"snapshot_id":\([0-9]*\).*/\1/p')
code=$(curl -s -o /tmp/swap-router-err.json -w '%{http_code}' -X POST "$BASE/quotes" \
  -H 'content-type: application/json' -d "{
    \"snapshot_id\":$DUST_SID,\"token_in\":\"AA\",\"token_out\":\"BB\",\"amount_in\":\"1\"}")
echo "http $code: $(cat /tmp/swap-router-err.json)"
[[ "$code" == "422" ]]
grep -q NO_FEASIBLE_PATH /tmp/swap-router-err.json

echo "==> 7. hop limit 1 forces direct-only; WETH route fine, WBTC has no direct pool -> 404"
code=$(curl -s -o /tmp/swap-router-err.json -w '%{http_code}' -X POST "$BASE/quotes" \
  -H 'content-type: application/json' -d "{
    \"snapshot_id\":$SID,\"token_in\":\"USDC\",\"token_out\":\"WBTC\",
    \"amount_in\":\"50000000000\",\"max_hops\":1}")
echo "http $code: $(cat /tmp/swap-router-err.json)"
[[ "$code" == "404" ]]
grep -q NO_PATH /tmp/swap-router-err.json

echo "==> 8. duplicate equivalent routes break tie by pool id"
# Custom snapshot: two identical 2-hop routes A->X->B and A->Y->B.
code2=$(curl -s -o /tmp/swap-router-tie.json -w '%{http_code}' -X POST "$BASE/snapshots" \
  -H 'content-type: application/json' -d '{
    "assets":[{"id":"A","decimals":18},{"id":"X","decimals":18},{"id":"Y","decimals":18},{"id":"B","decimals":18}],
    "pools":[
      {"id":"pAX","token0":"A","token1":"X","reserve0":"1000000000000","reserve1":"1000000000000","fee_bps":0},
      {"id":"pXB","token0":"X","token1":"B","reserve0":"1000000000000","reserve1":"1000000000000","fee_bps":0},
      {"id":"pAY","token0":"A","token1":"Y","reserve0":"1000000000000","reserve1":"1000000000000","fee_bps":0},
      {"id":"pYB","token0":"Y","token1":"B","reserve0":"1000000000000","reserve1":"1000000000000","fee_bps":0}]}')
[[ "$code2" == "201" ]]
TIE_SID=$(sed -n 's/.*"snapshot_id":\([0-9]*\).*/\1/p' /tmp/swap-router-tie.json)
[[ "$TIE_SID" =~ ^[0-9]+$ ]]
curl -sf -X POST "$BASE/quotes" -H 'content-type: application/json' -d "{
    \"snapshot_id\":$TIE_SID,\"token_in\":\"A\",\"token_out\":\"B\",\"amount_in\":\"123456\",\"max_hops\":2
  }" | python3 -c '
import json, sys
r = json.load(sys.stdin)
ids = [h["pool_id"] for h in r["hops"]]
assert ids == ["pAX","pXB"], ids
assert r["feasible_paths"] == 2, r
print("tie broken on pool ids:", ids)
'

echo "==> 9. latest snapshot endpoint"
curl -sf "$BASE/snapshots/latest" | grep -q "\"snapshot_id\":$TIE_SID"

echo "==> 10. overflowing reserves/amounts -> 422 OVERFLOW"
curl -sf -X POST "$BASE/snapshots" -H 'content-type: application/json' -d '{
  "assets":[{"id":"OX","decimals":38},{"id":"OY","decimals":38}],
  "pools":[{"id":"p","token0":"OX","token1":"OY",
    "reserve0":"340282366920938463463374607431768211454",
    "reserve1":"340282366920938463463374607431768211454","fee_bps":30}]}' >/dev/null
OVF_SID=$(curl -sf "$BASE/snapshots/latest" | sed -n 's/.*"snapshot_id":\([0-9]*\).*/\1/p')
code=$(curl -s -o /tmp/swap-router-err.json -w '%{http_code}' -X POST "$BASE/quotes" \
  -H 'content-type: application/json' -d "{
    \"snapshot_id\":$OVF_SID,\"token_in\":\"OX\",\"token_out\":\"OY\",
    \"amount_in\":\"340282366920938463463374607431768211454\"}")
echo "http $code: $(cat /tmp/swap-router-err.json)"
[[ "$code" == "422" ]]
grep -q '"OVERFLOW"' /tmp/swap-router-err.json

echo
echo "ALL ACCEPTANCE CHECKS PASSED"
