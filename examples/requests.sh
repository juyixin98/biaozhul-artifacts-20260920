#!/usr/bin/env bash
# Request samples for the dual-sb local HTTP validation endpoint.
# Start the server first:
#   ./target/release/dual-sb serve --dir ./mystore --addr 127.0.0.1:8080
set -euo pipefail
BASE="${BASE:-http://127.0.0.1:8080}"
j() { python3 -m json.tool; }

echo "== health =="
curl -s "$BASE/health" | j

echo "== put name=alice (gen 1) =="
curl -s -X POST "$BASE/kv/put" \
  -H 'Content-Type: application/json' \
  -d '{"key":"name","value":"alice"}' | j

echo "== put city=paris (gen 2) =="
curl -s -X POST "$BASE/kv/put" \
  -H 'Content-Type: application/json' \
  -d '{"key":"city","value":"paris"}' | j

echo "== overwrite name=bob (gen 3) =="
curl -s -X POST "$BASE/kv/put" \
  -H 'Content-Type: application/json' \
  -d '{"key":"name","value":"bob"}' | j

echo "== get name =="
curl -s "$BASE/kv/get?key=name" | j

echo "== list all =="
curl -s "$BASE/kv" | j

echo "== delete city (gen 4) =="
curl -s -X POST "$BASE/kv/delete" \
  -H 'Content-Type: application/json' \
  -d '{"key":"city"}' | j

echo "== get city after delete (404) =="
curl -s -o /dev/null -w "HTTP %{http_code}\n" "$BASE/kv/get?key=city"

# --- crash-injection demos (in-memory; never touch ./mystore) ---
echo "== crash matrix summary =="
curl -s "$BASE/demo/matrix" | \
  python3 -c 'import sys,json;d=json.load(sys.stdin);print(d["title"]);print("passed",d["passed"],"/",d["cases"])'

echo "== single case: half-page torn root during commit 3 =="
curl -s -X POST "$BASE/demo/crash" \
  -H 'Content-Type: application/json' \
  -d '{"point":"RootWriteEnd","torn":"Half","round":3}' | j

echo "== named acceptance scenarios =="
curl -s "$BASE/demo/scenarios" | \
  python3 -c 'import sys,json;[print("-",s["name"],"=>",s["outcome"]) for s in json.load(sys.stdin)["scenarios"]]'
