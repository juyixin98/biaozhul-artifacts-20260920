#!/usr/bin/env bash
# End-to-end acceptance run:
#   1. (re)generate the offline trusted sample
#   2. start the three faulty stub nodes (timeout / mid-chain corruption / bad-parent fork, all lying about height)
#   3. run a fresh sync into a clean SQLite DB and require a full match
#   4. run again against the same DB to demonstrate checkpoint-driven resume
#   5. print the provenance evidence showing each fault was detected and worked around
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

LENGTH="${LENGTH:-32}"
SEGMENT="${SEGMENT:-8}"
PARALLEL="${PARALLEL:-4}"
TIMEOUT="${TIMEOUT:-600ms}"
ATTEMPTS="${ATTEMPTS:-6}"

A=50071; B=50072; C=50073
mkdir -p data bin
rm -f data/sync.db data/evidence.json data/summary.json

echo "== build =="
go build -mod=vendor -o bin/genchain ./cmd/genchain
go build -mod=vendor -o bin/node      ./cmd/node
go build -mod=vendor -o bin/syncer    ./cmd/syncer

echo "== generate trusted sample (length=$LENGTH) =="
./bin/genchain --length "$LENGTH" --out testdata/trusted_sample.json

PIDS=()
cleanup() { kill "${PIDS[@]}" 2>/dev/null || true; }
trap cleanup EXIT

echo "== start stub nodes =="
./bin/node --addr 127.0.0.1:$A --config testdata/node_alpha.json >data/alpha.log 2>&1 & PIDS+=($!)
./bin/node --addr 127.0.0.1:$B --config testdata/node_beta.json  >data/beta.log  2>&1 & PIDS+=($!)
./bin/node --addr 127.0.0.1:$C --config testdata/node_gamma.json >data/gamma.log 2>&1 & PIDS+=($!)
sleep 1

NODE_ARGS=(--node "alpha=127.0.0.1:$A" --node "beta=127.0.0.1:$B" --node "gamma=127.0.0.1:$C")
COMMON=(--db data/sync.db --segment "$SEGMENT" --parallel "$PARALLEL" --timeout "$TIMEOUT" --attempts "$ATTEMPTS")

echo
echo "== fresh sync =="
./bin/syncer "${COMMON[@]}" "${NODE_ARGS[@]}"

echo
echo "== resume/restart sync (same DB) =="
./bin/syncer "${COMMON[@]}" "${NODE_ARGS[@]}" --evidence data/evidence_resume.json --summary data/summary_resume.json

echo
echo "== evidence of detected faults (fresh run) =="
python3 - <<'PY'
import json
rows = json.load(open("data/evidence.json"))
want = {"timeout", "hash_mismatch", "parent_mismatch", "advertised_mismatch", "published"}
for r in rows:
    if r["outcome"] in want:
        print(f"  {r['node_id']:<6} h{r['start_height']}-{r['end_height']:<3} attempt={r['attempt']} {r['outcome']:<20} {r['detail'][:60]}")
PY

echo
echo "== verdict =="
python3 - <<'PY'
import json, sys
s = json.load(open("data/summary.json"))
ok = s.get("matches_sample") is True and s.get("last_height") == 32
print("summary matches trusted sample:", s.get("matches_sample"), "tip:", s.get("last_height"))
sys.exit(0 if ok else 1)
PY
echo
echo "ACCEPTANCE OK"
