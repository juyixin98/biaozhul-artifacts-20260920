#!/usr/bin/env bash
# Request samples for the transparent log service.
#
# Prerequisites:
#   python3 -m pip install -r requirements.txt
#   python3 -m tl.server --port 8088 --data-dir ./.tl-data
#
# All hashes/proofs are standard base64 (with padding) JSON strings.
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8088}"

echo "--- health ---"
curl -s "$BASE/health"; echo

echo "--- public verification key (Ed25519, raw base64 + PEM) ---"
curl -s "$BASE/v1/public-key"; echo

echo "--- append three entries (text convenience form) ---"
curl -s -X POST "$BASE/v1/entries" -H 'Content-Type: application/json' \
  -d '{"text":"hello world"}'; echo
curl -s -X POST "$BASE/v1/entries" -H 'Content-Type: application/json' \
  -d '{"text":"second entry"}'; echo

echo "--- append a raw-bytes entry (data_b64) ---"
curl -s -X POST "$BASE/v1/entries" -H 'Content-Type: application/json' \
  -d '{"data_b64":"AAECAwQ="}'; echo

echo "--- current signed tree head ---"
curl -s "$BASE/v1/sth"; echo

echo "--- fetch entry 0 ---"
curl -s "$BASE/v1/entries?index=0"; echo

echo "--- inclusion proof for leaf 1 against current tree ---"
curl -s "$BASE/v1/proof/inclusion?leaf_index=1"; echo

echo "--- inclusion proof for leaf 0 against historical size 1 ---"
curl -s "$BASE/v1/proof/inclusion?leaf_index=0&tree_size=1"; echo

echo "--- consistency proof: old size 2 -> current ---"
curl -s "$BASE/v1/proof/consistency?old_size=2"; echo

echo "--- consistency proof: old size 1 -> new size 3 ---"
curl -s "$BASE/v1/proof/consistency?old_size=1&new_size=3"; echo

echo "--- verify an inclusion proof server-side (POST JSON) ---"
# Pull a real proof then feed it back to /v1/verify.
PROOF=$(curl -s "$BASE/v1/proof/inclusion?leaf_index=0")
python3 - "$BASE" "$PROOF" <<'PY'
import json, sys, urllib.request
base, proof = sys.argv[1], json.loads(sys.argv[2])
body = {
    "kind": "inclusion",
    "leaf_index": proof["leaf_index"],
    "tree_size": proof["tree_size"],
    "leaf_hash": proof["leaf_hash"],
    "root_hash": proof["root_hash"],
    "proof": proof["proof"],
}
req = urllib.request.Request(
    base + "/v1/verify", data=json.dumps(body).encode(),
    headers={"Content-Type": "application/json"}, method="POST")
print(urllib.request.urlopen(req).read().decode())
PY

echo "--- error samples ---"
curl -s "$BASE/v1/proof/inclusion?leaf_index=999"; echo   # index out of range -> 400
curl -s "$BASE/v1/entries?index=999"; echo                # missing entry -> 404
