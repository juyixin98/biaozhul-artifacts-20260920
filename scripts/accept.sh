#!/usr/bin/env bash
# One-shot acceptance: build from scratch, run every unit/integration test,
# then exercise the real HTTP service end to end with curl, and the offline
# CLI against the bundled example (including per-beam reference verification
# and numeric CSV/JSON export).
set -euo pipefail

cd "$(dirname "$0")/.."
PORT="${PORT:-18080}"
BASE="http://127.0.0.1:${PORT}"
TMP="$(mktemp -d)"
trap 'kill "${SRV_PID:-0}" 2>/dev/null || true; rm -rf "$TMP"' EXIT

echo "== 1. configure + build =="
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j"$(nproc)"

echo "== 2. ctest (sha256 vectors, grid rules, http e2e) =="
( cd build && ctest --output-on-failure )

echo "== 3. offline CLI on bundled example =="
./build/gridfusion verify --map examples/map.json --scans examples/scans.json
./build/gridfusion replay --map examples/map.json --scans examples/scans.json \
    --bbox -o "$TMP/grid.json"
./build/gridfusion replay --map examples/map.json --scans examples/scans.json \
    --logodds --bbox --csv -o "$TMP/grid.csv"
echo "-- CSV head --"; head -4 "$TMP/grid.csv"
echo "-- JSON summary --"
python3 - "$TMP/grid.json" <<'PY'
import json,sys
g=json.load(open(sys.argv[1]))
rows=g["rows"]
vals=[v for r in rows for v in r if v is not None]
print("version:", g["version"])
print("crop:", g["width"], "x", g["height"],
      "observed:", len(vals),
      "max p:", round(max(vals),4), "min p:", round(min(vals),4))
assert g["width"] and g["height"] and vals
assert any(v > 0.9 for v in vals)      # saturated wall cells
assert any(v is None for r in rows for v in r)  # unknown cells present
PY

echo "== 4. start HTTP service on port ${PORT} =="
./build/gridfusion-server --host 127.0.0.1 --port "${PORT}" &
SRV_PID=$!
for _ in $(seq 1 50); do curl -fsS "${BASE}/health" >/dev/null 2>&1 && break; sleep 0.1; done
curl -fsS "${BASE}/health" | head -c 200; echo

echo "== 5. create map, check version binding =="
V=$(curl -fsS -X POST "${BASE}/api/maps" -H 'Content-Type: application/json' \
      --data @examples/map.json | python3 -c 'import json,sys;print(json.load(sys.stdin)["version"])')
echo "version: ${V}"
test "${#V}" -eq 64
# Same geometry reuses the id; changed resolution yields a different one.
V2=$(curl -fsS -X POST "${BASE}/api/maps" -H 'Content-Type: application/json' \
      --data @examples/map.json | python3 -c 'import json,sys;print(json.load(sys.stdin)["version"])')
test "$V" = "$V2"
sed 's/"resolution": 0.1/"resolution": 0.2/' examples/map.json > "$TMP/m2.json"
V3=$(curl -fsS -X POST "${BASE}/api/maps" -H 'Content-Type: application/json' \
      --data @"$TMP/m2.json" | python3 -c 'import json,sys;print(json.load(sys.stdin)["version"])')
test "$V" != "$V3"

echo "== 6. ingest example scans over HTTP =="
python3 - "$TMP/scans.json" <<'PY'
import json,sys
scans=json.load(open("examples/scans.json"))["scans"]
json.dump(scans, open(sys.argv[1],"w"))
PY
python3 - "$TMP" "$BASE/api/maps/$V" <<'PY'
import json,sys,urllib.request
tmp,base=sys.argv[1],sys.argv[2]
scans=json.load(open(tmp+"/scans.json"))
for s in scans:
    req=urllib.request.Request(base+"/scans",
        data=json.dumps(s).encode(), headers={"Content-Type":"application/json"})
    st=json.load(urllib.request.urlopen(req))
    print("  scan:", st["hits"], "hits,", st["no_returns"],
          "no-returns,", st["beams_clipped"], "clipped")
    assert st["no_returns"] > 0
# verify endpoint: production traversal vs per-beam reference
req=urllib.request.Request(base+"/verify",
    data=json.dumps(scans).encode(), headers={"Content-Type":"application/json"})
v=json.load(urllib.request.urlopen(req))
print("  verify:", v)
assert v["matches_reference"] and v["differing_cells"] == 0
PY

echo "== 7. export numeric grid (JSON + CSV, probability + logodds) =="
curl -fsS "${BASE}/api/maps/${V}/grid?bbox=1" -o "$TMP/http_grid.json"
curl -fsS "${BASE}/api/maps/${V}/grid.csv?logodds=1" -o "$TMP/http_grid.csv"
head -1 "$TMP/http_grid.csv"
python3 - "$TMP/http_grid.json" <<'PY'
import json,sys
g=json.load(open(sys.argv[1]))
vals=[v for r in g["rows"] for v in r if v is not None]
assert max(vals) > 0.96, max(vals)          # wall saturated at l_max
assert any(v is None for r in g["rows"] for v in r)  # unknown != 0.5
print("  exported crop", g["width"], "x", g["height"],
      "max p =", round(max(vals),4))
PY

echo
echo "ACCEPTANCE OK"
