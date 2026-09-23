#!/usr/bin/env bash
# End-to-end demonstration / verification script.
# Starts the server itself on an ephemeral port, exercises every route and
# the fault injector, prints JSON responses, and shuts the server down.
set -u

BIN=${BIN:-./target/release/tsblock}
DATA=${DATA:-./demo-data}
rm -rf "$DATA"

"$BIN" --addr 127.0.0.1:0 --data-dir "$DATA" --block-size 1000 --fault >/tmp/tsblock-demo.log 2>&1 &
PID=$!
trap 'kill $PID 2>/dev/null || true' EXIT

# Read the ephemeral port from the startup log.
for _ in $(seq 1 50); do
  ADDR=$(grep -o '127.0.0.1:[0-9]*' /tmp/tsblock-demo.log | head -1 || true)
  [ -n "$ADDR" ] && break
  sleep 0.05
done
BASE="http://$ADDR"
echo "server: $BASE (pid $PID)"

j() { python3 -m json.tool 2>/dev/null || cat; }

echo; echo "== health =="
curl -s "$BASE/health"

echo; echo "== create reject + buffer series =="
curl -s -X POST "$BASE/series/cpu?policy=reject&block_size=1000"
curl -s -X POST "$BASE/series/late?policy=buffer"

echo; echo "== write 10000 points: ts every ~60s, value random walk =="
python3 - "$DATA" <<'PY' >/tmp/points.txt
import sys, random
random.seed(7)
ts=1_700_000_000; v=0
for i in range(10_000):
    ts += 0 if (i and i % 1000 == 0) else 58 + random.randrange(7)
    v += random.randrange(-3,4)
    print(f"{ts},{v}")
PY
curl -s -X POST "$BASE/series/cpu/points?sync=true" --data-binary @/tmp/points.txt | j
curl -s -X POST "$BASE/series/late/points" --data-binary @/tmp/points.txt >/dev/null

echo; echo "== stats (real compression ratio) =="
curl -s "$BASE/series/cpu/stats" | j

echo; echo "== boundary queries =="
curl -s "$BASE/series/cpu/range?start=1700000000&end=1700000060" | j
curl -s "$BASE/series/cpu/range?start=-9223372036854775808&end=9223372036854775807" \
  | python3 -c 'import sys,json; d=json.load(sys.stdin); print("full-range count:",d["count"],"blocks_scanned:",d["blocks_scanned"])'

echo; echo "== out-of-order rejection (expect 409) =="
curl -s -o /tmp/ooo.json -w "HTTP %{http_code}\n" -X POST "$BASE/series/cpu/points" \
  --data-binary $'9999999999,1\n1,2'
cat /tmp/ooo.json; echo

echo; echo "== late points buffered, invisible, then drained =="
curl -s -X POST "$BASE/series/late/points" --data-binary $'1500000000,-7\n1600000000,-8'
echo
curl -s "$BASE/series/late/buffered"
curl -s -X POST "$BASE/series/late/drain" | j

echo; echo "== fault injection: ENOSPC then retry =="
curl -s -X POST "$BASE/series/victim?policy=reject&block_size=10" >/dev/null
curl -s -X POST "$BASE/dev/fault" --data-binary $'write_limit=32'
echo
curl -s -o /tmp/f.json -w "write under fault -> HTTP %{http_code}\n" \
  -X POST "$BASE/series/victim/points" \
  --data-binary "$(python3 -c 'print("\n".join(f"{i},{i}" for i in range(10)))')"
cat /tmp/f.json; echo
curl -s -X POST "$BASE/dev/fault" --data-binary clear >/dev/null
curl -s -X POST "$BASE/series/victim/flush" | j

echo; echo "== restart: torn tail recovery =="
kill $PID; wait $PID 2>/dev/null
trap - EXIT
ls -l "$DATA"
"$BIN" --addr 127.0.0.1:0 --data-dir "$DATA" --fault >/tmp/tsblock-demo2.log 2>&1 &
PID=$!
trap 'kill $PID 2>/dev/null || true' EXIT
for _ in $(seq 1 50); do
  ADDR2=$(grep -o '127.0.0.1:[0-9]*' /tmp/tsblock-demo2.log | head -1 || true)
  [ -n "$ADDR2" ] && break
  sleep 0.05
done
echo "restarted on http://$ADDR2"
curl -s "http://$ADDR2/series/cpu/stats" | j
curl -s "http://$ADDR2/series/cpu/range?start=1700000000&end=1700000000" | j
echo; echo "demo complete"
