#!/usr/bin/env bash
# End-to-end HLC acceptance demo over real HTTP (standard library only).
#
# Starts three local nodes:
#   A :$PORT_A  normal node (drift 1000ms)
#   B :$PORT_B  normal node (drift 1000ms)
#   C :$PORT_C  generous drift cap, tiny overflow wait (100ms) -> bounded 503
#
# All correctness-relevant checks are also covered deterministically by
# `go test ./...`; this script shows the live HTTP behaviour.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN="$ROOT/bin/hlc-server"
PORT_A="${PORT_A:-19091}"
PORT_B="${PORT_B:-19092}"
PORT_C="${PORT_C:-19093}"
MAX_DRIFT=1000
HUGE_DRIFT=9223372036854775807
MAX_LOGICAL=4294967295

if [[ ! -x "$BIN" ]]; then
  (cd "$ROOT" && go build -o "$BIN" ./cmd/hlc-server)
fi

A="http://127.0.0.1:$PORT_A"; B="http://127.0.0.1:$PORT_B"; C="http://127.0.0.1:$PORT_C"
PIDS=()
start() { # addr node drift overflowWaitMs
  "$BIN" --addr="127.0.0.1:$1" --node="$2" --drift="$3" \
    --max-logical="$MAX_LOGICAL" --overflow-wait="$4" >"/tmp/hlc-demo-$2.log" 2>&1 &
  PIDS+=($!)
}
cleanup() { for p in "${PIDS[@]:-}"; do kill "$p" >/dev/null 2>&1 || true; done; }
trap cleanup EXIT

start "$PORT_A" A "$MAX_DRIFT" 250
start "$PORT_B" B "$MAX_DRIFT" 250
start "$PORT_C" C "$HUGE_DRIFT" 100
sleep 1
for u in "$A/healthz" "$B/healthz" "$C/healthz"; do
  curl -sf --max-time 2 "$u" >/dev/null || { echo "server $u not healthy"; exit 1; }
done

line() { printf '\n==== %s ====\n' "$1"; }
wire() { python3 -c 'import sys,json;print(json.load(sys.stdin)["timestamp"]["wire"])'; }

line "1. health (includes physical_now_ms)"
curl -s "$A/healthz"; echo

line "2. same-millisecond burst: 64 concurrent ticks"
tmpd=$(mktemp -d)
curl_pids=()
for i in $(seq 1 64); do
  curl -s -X POST "$A/v1/tick" >"$tmpd/$i" &
  curl_pids+=("$!")
done
for cp in "${curl_pids[@]}"; do wait "$cp"; done
python3 - "$tmpd" </dev/null <<'PY'
import json, os, sys
rows=[json.load(open(os.path.join(sys.argv[1],f)))["timestamp"] for f in os.listdir(sys.argv[1])]
rows.sort(key=lambda r:(r["physical_ms"],r["logical"]))
inc=all((rows[i-1]["physical_ms"],rows[i-1]["logical"])<(rows[i]["physical_ms"],rows[i]["logical"]) for i in range(1,len(rows)))
print(f"responses={len(rows)} unique={len({(r['physical_ms'],r['logical']) for r in rows})} strictly_increasing={inc}")
PY
rm -rf "$tmpd"

line "3. causal chain A -> B -> A (timestamps strictly increase)"
MA=$(curl -s -X POST "$A/v1/tick"); echo "A ticks    : $(echo "$MA"|wire)"
RB=$(curl -s -X POST "$B/v1/receive" -d "{\"timestamp\":$(echo "$MA" | python3 -c 'import sys,json;print(json.dumps(json.load(sys.stdin)["timestamp"]))')}")
echo "B receives : $(echo "$RB"|wire)"
MB=$(curl -s -X POST "$B/v1/tick"); echo "B ticks    : $(echo "$MB"|wire)"
RA=$(curl -s -X POST "$A/v1/receive" -d "{\"timestamp\":$(echo "$MB" | python3 -c 'import sys,json;print(json.dumps(json.load(sys.stdin)["timestamp"]))')}")
echo "A receives : $(echo "$RA"|wire)"

line "4. future drift (+1h, cap 1000ms) -> 422 future_drift"
FAR=$(( $(date +%s%3N) + 3600000 ))
curl -s -w '\nHTTP %{http_code}\n' -X POST "$A/v1/receive" \
  -d "{\"timestamp\":{\"physical_ms\":$FAR,\"logical\":0,\"node_id\":\"future\"}}"

line "5. boundary exactly +1000ms -> 200"
EDGE=$(( $(date +%s%3N) + 1000 ))
curl -s -o /dev/null -w 'HTTP %{http_code}\n' -X POST "$A/v1/receive" \
  -d "{\"timestamp\":{\"physical_ms\":$EDGE,\"logical\":7,\"node_id\":\"edge\"}}"

line "6. large exact integers (logical 2^32-3 -> 2^32-2), no precision loss"
BP=$(( $(curl -s "$B/healthz" | python3 -c 'import sys,json;print(json.load(sys.stdin)["physical_now_ms"])') + 800 ))
curl -s -X POST "$B/v1/receive" -d "{\"timestamp\":{\"physical_ms\":$BP,\"logical\":4294967293,\"node_id\":\"big\"}}" \
 | python3 -c 'import sys,json;t=json.load(sys.stdin)["timestamp"];print(t["wire"]);assert t["physical_ms"]=='"$BP"' and t["logical"]==4294967294'

line "7. canonical text wire form (JSON-quoted body)"
# Sample physical time immediately before the request and aim 900ms into the
# (1000ms) future so round-trip latency cannot push us back onto the
# physical-reset branch; the remote logical still merges to 2^32-2.
BP7=$(( $(curl -s "$B/healthz" | python3 -c 'import sys,json;print(json.load(sys.stdin)["physical_now_ms"])') + 900 ))
curl -s -X POST "$B/v1/receive" --data-binary "\"hlc://wire/$BP7:4294967293\"" \
 | python3 -c 'import sys,json;t=json.load(sys.stdin)["timestamp"];print(t["wire"]);assert t["physical_ms"]=='"$BP7"' and t["logical"]==4294967294'

line "8. bounded overflow: logical pinned at limit, far physical -> 503 after ~100ms"
curl -s -w '\nHTTP %{http_code}\n' --max-time 10 -X POST "$C/v1/receive" \
  --data-binary 'hlc://wire-node/4611686018427387903:4294967295'

line "9. status counters on A"
curl -s "$A/v1/status" | python3 -m json.tool
