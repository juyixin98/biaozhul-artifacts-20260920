#!/usr/bin/env bash
# examples/demo.sh — end-to-end HLC acceptance demo over real HTTP.
#
# Brings up three local server instances with injected scenarios:
#   node-A :19190  normal node
#   node-B :19191  normal node
#   node-C :19192  generous drift cap (to absorb an otherwise-rejected value),
#                  tiny overflow-wait (to show bounded overflow failure)
#
# Requires: bash, curl, python3, and the built binary (run `go build -o
# /tmp/hlc-server .` first, or set HLC_BIN to an existing binary).
#
# Set HLC_NO_SPAWN=1 to connect to servers you started yourself instead of
# having the script spawn them (useful in restricted shells).
set -u

BIN="${HLC_BIN:-/tmp/hlc-server}"
A="http://127.0.0.1:19190"
B="http://127.0.0.1:19191"
C="http://127.0.0.1:19192"
PIDS=()

cleanup() {
  for pid in "${PIDS[@]:-}"; do kill "$pid" >/dev/null 2>&1 || true; done
}
trap cleanup EXIT

start() { # addr node drift maxLogical overflowWaitMs
  "$BIN" --addr="127.0.0.1:$1" --node="$2" --drift="$3" \
    --max-logical="$4" --overflow-wait="$5" >"/tmp/hlc-$2.log" 2>&1 &
  PIDS+=($!)
}

if [[ "${HLC_NO_SPAWN:-0}" != "1" ]]; then
  if [[ ! -x "$BIN" ]]; then
    echo "building $BIN ..."
    (cd "$(dirname "$0")/.." && go build -o "$BIN" .)
  fi

  start 19190 node-A 1000 4294967295 250
  start 19191 node-B 1000 4294967295 250
  start 19192 node-C 9223372036854775807 4294967295 100
  sleep 1

  # Fail fast if any server failed to bind.
  for url in "$A/healthz" "$B/healthz" "$C/healthz"; do
    if ! curl -sf --max-time 2 "$url" >/dev/null; then
      echo "server at $url did not become healthy; logs:" >&2
      cat /tmp/hlc-node-*.log >&2 || true
      exit 1
    fi
  done
fi

line() { printf '\n==== %s ====\n' "$1"; }

line "1. health"
curl -s "$A/healthz"; echo

line "2. same-millisecond burst: 64 concurrent ticks against one server"
tmpd=$(mktemp -d)
for i in $(seq 1 64); do
  curl -s -X POST "$A/v1/tick" >"$tmpd/$i" &
done
wait
python3 - "$tmpd" <<'PY'
import json, os, sys
d = sys.argv[1]
rows = [json.load(open(os.path.join(d, f)))["timestamp"] for f in os.listdir(d)]
rows.sort(key=lambda r: (r["physical_ms"], r["logical"]))
inc = all((rows[i-1]["physical_ms"], rows[i-1]["logical"]) <
          (rows[i]["physical_ms"], rows[i]["logical"]) for i in range(1, len(rows)))
phys = {}
for r in rows:
    phys[r["physical_ms"]] = phys.get(r["physical_ms"], 0) + 1
busiest = max(phys, key=phys.get)
print(f"responses          : {len(rows)}")
print(f"unique timestamps  : {len({(r['physical_ms'], r['logical']) for r in rows})}")
print(f"distinct phys ms   : {len(phys)}")
print(f"busiest ms         : {busiest} held {phys[busiest]} events (logical 0..{phys[busiest]-1})")
print(f"strictly increasing: {inc}")
PY
rm -rf "$tmpd"

line "3. causal message chains (timestamps strictly increase)"
echo "  3a. wall-clock separated chain A -> B -> A:"
MA=$(curl -s -X POST "$A/v1/tick")
echo "    A sends    : $(echo "$MA" | python3 -c 'import sys,json;print(json.load(sys.stdin)["timestamp"]["wire"])')"
RB=$(curl -s -X POST "$B/v1/receive" -H 'Content-Type: application/json' \
  -d "{\"timestamp\":$(echo "$MA" | python3 -c 'import sys,json;print(json.dumps(json.load(sys.stdin)["timestamp"]))')}")
echo "    B receives : $(echo "$RB" | python3 -c 'import sys,json;print(json.load(sys.stdin)["timestamp"]["wire"])')"
MB=$(curl -s -X POST "$B/v1/tick")
echo "    B sends    : $(echo "$MB" | python3 -c 'import sys,json;print(json.load(sys.stdin)["timestamp"]["wire"])')"
RA=$(curl -s -X POST "$A/v1/receive" -H 'Content-Type: application/json' \
  -d "{\"timestamp\":$(echo "$MB" | python3 -c 'import sys,json;print(json.dumps(json.load(sys.stdin)["timestamp"]))')}")
echo "    A receives : $(echo "$RA" | python3 -c 'import sys,json;print(json.load(sys.stdin)["timestamp"]["wire"])')"

echo "  3b. SAME-millisecond causal exchange (timestamp ~400ms ahead of B's"
echo "      wall clock but inside the drift cap: physical part is pinned to the"
echo "      remote value and logical counters climb instead of resetting):"
BP=$(curl -s "$B/healthz" | python3 -c 'import sys,json;print(json.load(sys.stdin)["physical_now_ms"]+400)')
S1="{\"physical_ms\":$BP,\"logical\":10,\"node_id\":\"A\"}"
X1=$(curl -s -X POST "$B/v1/receive" -H 'Content-Type: application/json' -d "{\"timestamp\":$S1}")
echo "    B receives hlc://A/$BP:10 -> $(echo "$X1" | python3 -c 'import sys,json;print(json.load(sys.stdin)["timestamp"]["wire"])')"
X2=$(curl -s -X POST "$B/v1/tick")
echo "    B local tick              -> $(echo "$X2" | python3 -c 'import sys,json;print(json.load(sys.stdin)["timestamp"]["wire"])')"
S2=$(echo "$X2" | python3 -c 'import sys,json;print(json.dumps(json.load(sys.stdin)["timestamp"]))')
X3=$(curl -s -X POST "$B/v1/receive" -H 'Content-Type: application/json' -d "{\"timestamp\":$S2}")
echo "    B receives own value back -> $(echo "$X3" | python3 -c 'import sys,json;print(json.load(sys.stdin)["timestamp"]["wire"])')"

line "4. future drift rejection (1 hour ahead, cap 1000ms) -> 422"
FAR=$(( $(date +%s%3N) + 3600000 ))
curl -s -w '\nHTTP %{http_code}\n' -X POST "$A/v1/receive" -H 'Content-Type: application/json' \
  -d "{\"timestamp\":{\"physical_ms\":$FAR,\"logical\":0,\"node_id\":\"future\"}}"

line "5. boundary: exactly 1000ms ahead is accepted"
EDGE=$(( $(date +%s%3N) + 1000 ))
curl -s -w '\nHTTP %{http_code}\n' -X POST "$A/v1/receive" -H 'Content-Type: application/json' \
  -d "{\"timestamp\":{\"physical_ms\":$EDGE,\"logical\":7,\"node_id\":\"edge\"}}"

line "6. large exact integers over JSON, no precision loss"
# Place physical ~400ms ahead (inside drift cap) so the merge keeps the remote
# physical part; logical 2^32-3 merges to 2^32-2 without overflow.
BP=$(curl -s "$B/healthz" | python3 -c 'import sys,json;print(json.load(sys.stdin)["physical_now_ms"]+400)')
echo "  request : {physical_ms:$BP, logical:4294967293} (2^32-3)"
curl -s -X POST "$B/v1/receive" -H 'Content-Type: application/json' \
  -d "{\"physical_ms\":$BP,\"logical\":4294967293,\"node_id\":\"big\"}" \
  | python3 -c 'import sys,json
t=json.load(sys.stdin)["timestamp"]
print("  response:", t["wire"])
assert t["physical_ms"]=='"$BP"' and t["logical"]==4294967294, t
print("  exact integers preserved through JSON request/response")'

line "7. canonical text wire form (JSON-quoted body), no precision loss"
curl -s -X POST "$B/v1/receive" \
  --data-binary "\"hlc://wire/$((BP+1)):4294967293\"" \
  | python3 -c 'import sys,json
t=json.load(sys.stdin)["timestamp"]
print("  response:", t["wire"])
assert t["physical_ms"]=='"$((BP+1))"' and t["logical"]==4294967294, t
print("  hlc:// text form parsed and echoed exactly")'

line "8. bounded overflow: remote logical pinned at limit, far physical -> 503"
curl -s -w '\nHTTP %{http_code} after ~100ms, not a hang\n' --max-time 10 \
  -X POST "$C/v1/receive" -H 'Content-Type: application/json' \
  -d '"hlc://wire-node/4611686018427387903:4294967295"'

line "9. status counters on node-A"
curl -s "$A/v1/status" | python3 -m json.tool

echo
echo "demo complete"
