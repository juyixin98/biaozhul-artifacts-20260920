#!/usr/bin/env bash
# End-to-end demo: create two shards, insert, refuse incompatible merge,
# merge compatible shards, query quantiles/ranks, serialize/deserialize.
# Requires: running service (default http://localhost:8080) and python3.
set -euo pipefail
B="${1:-http://localhost:8080}"

echo "# service: $B"
echo
echo "## 1) create two compatible shards (eps=0.01, universe=100000)"
curl -s -X POST "$B/summaries" -H 'Content-Type: application/json' \
  -d '{"id":"demoA","eps":0.01,"universe":100000}'; echo
curl -s -X POST "$B/summaries" -H 'Content-Type: application/json' \
  -d '{"id":"demoB","eps":0.01,"universe":100000}'; echo

echo
echo "## 2) insert 0..39999 into A, 40000..79999 into B"
python3 - "$B" <<'EOF'
import json, sys, urllib.request
B = sys.argv[1]
def post(path, obj):
    req = urllib.request.Request(B + path, data=json.dumps(obj).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    return urllib.request.urlopen(req).read().decode()
print(post("/summaries/demoA/values", {"values": list(range(0, 40000))}))
print(post("/summaries/demoB/values", {"values": list(range(40000, 80000))}))
EOF

echo
echo "## 3) create incompatible shard (eps=0.05), merge must be rejected (409)"
curl -s -X POST "$B/summaries" -H 'Content-Type: application/json' \
  -d '{"id":"demoBad","eps":0.05,"universe":100000}' >/dev/null
curl -s -w "\nHTTP %{http_code}\n" -X POST "$B/summaries/merge" \
  -H 'Content-Type: application/json' \
  -d '{"target":"demoA","sources":["demoBad"]}'

echo
echo "## 4) merge B into A -> summary of 0..79999"
curl -s -X POST "$B/summaries/merge" -H 'Content-Type: application/json' \
  -d '{"target":"demoA","sources":["demoB"]}'; echo

echo
echo "## 5) quantiles and a rank query"
for q in 0.5 0.9 0.99; do
  printf 'q=%s -> ' "$q"
  curl -s "$B/summaries/demoA/quantile?q=$q"; echo
done
printf 'rank(x=29999, exact F=30000) -> '
curl -s "$B/summaries/demoA/rank?x=29999"; echo

echo
echo "## 6) serialize A, deserialize as demoCopy"
python3 - "$B" <<'EOF'
import json, sys, urllib.request
B = sys.argv[1]
data = json.loads(urllib.request.urlopen(B + "/summaries/demoA/serialize").read())
print("wire bytes:", data["bytes"], "format:", data["format"])
req = urllib.request.Request(B + "/summaries/deserialize",
    data=json.dumps({"id": "demoCopy", "data": data["data"]}).encode(),
    headers={"Content-Type": "application/json"}, method="POST")
print(urllib.request.urlopen(req).read().decode())
EOF

echo
echo "## 7) cleanup"
for id in demoA demoB demoBad demoCopy; do
  curl -s -X DELETE "$B/summaries/$id"; echo
done
