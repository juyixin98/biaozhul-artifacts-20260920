#!/usr/bin/env bash
# End-to-end smoke test: build, start the server, exercise every example,
# print a compact (satisfiable / conflicts / cycles / exhaustive) summary.
set -euo pipefail

cd "$(dirname "$0")/.."
PORT="${PORT:-8099}"
BASE="http://127.0.0.1:${PORT}"

cargo build --quiet
./target/debug/license-propagation &
SRV=$!
trap 'kill $SRV 2>/dev/null || true' EXIT

# wait for readiness
for _ in $(seq 1 50); do
  curl -sf "$BASE/health" >/dev/null 2>&1 && break
  sleep 0.1
done

echo "== validate-expression =="
curl -s -X POST "$BASE/api/validate-expression" \
  -H 'content-type: application/json' \
  -d '{"expression": "MIT OR (Apache-2.0 AND GPL-2.0-only WITH Classpath-exception-2.0)"}'
echo

for f in examples/*.json; do
  echo "== $f =="
  curl -s -X POST "$BASE/api/analyze" \
    -H 'content-type: application/json' \
    --data-binary "@$f" \
    | python3 -c '
import json, sys
r = json.load(sys.stdin)
print("  satisfiable:", r["satisfiable"])
if r.get("selection"):
    print("  chosen:", {n["id"]: n["chosen"]["expression"] for n in r["selection"] if n.get("chosen")})
if r.get("conflicts"):
    for c in r["conflicts"]:
        if c["kind"] == "copyleft":
            hops = c.get("path", [])
            path = " -> ".join([hops[0]["from"]] + [h["to"] for h in hops]) if hops else "(within package)"
            print("  conflict:", c["source"], "->", c["target"],
                  "(" + c["source_license"], "vs", c["target_license"] + ")",
                  "path:", path)
        else:
            print("  conflict (allow):", c["package"], c["license"], "-", c["detail"])
if r.get("cycles"):
    print("  cycles:", [[n for n in cy["nodes"]] for cy in r["cycles"]])
if r.get("active_obligations"):
    print("  obligations:", [(o["source"], o["source_license"], o["strength"]) for o in r["active_obligations"]])
if r.get("exhaustive"):
    print("  exhaustive:", r["exhaustive"])
'
done
