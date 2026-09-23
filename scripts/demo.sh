#!/usr/bin/env bash
# End-to-end acceptance demo:
#   1. starts the service on a fresh RocksDB directory
#   2. applies the two example batches
#   3. fetches existence + non-existence proofs (incl. boundary cases)
#   4. cross-checks every proof with BOTH independent verifiers
#      (Rust examples/verify.rs and Python scripts/verify.py)
#   5. restarts the service and proves state/roots survive
#
# Usage: ./scripts/demo.sh
set -euo pipefail
cd "$(dirname "$0")/.."

DB="data/demo-db"
ADDR="127.0.0.1:19000"
BASE="http://${ADDR}"
OUT="data/demo-out"
# Some environments force a proxy even for 127.0.0.1; all demo traffic is local.
export NO_PROXY="127.0.0.1,localhost" no_proxy="127.0.0.1,localhost"
unset ALL_PROXY all_proxy HTTP_PROXY HTTPS_PROXY http_proxy https_proxy
rm -rf "$DB" "$OUT"
mkdir -p "$OUT"

cargo build --release --example verify >/dev/null
VERIFY=(cargo run --release -q --example verify --)

say() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }

# Both verifiers must accept.
check_rust() { "${VERIFY[@]}" "$@"; }
check_py()   { python3 scripts/verify.py "$@"; }

say "start service"
./target/release/merkle-proof-service --db "$DB" --listen "$ADDR" \
    >"$OUT/server.log" 2>&1 &
PID=$!
trap 'kill $PID 2>/dev/null || true' EXIT
for _ in $(seq 1 50); do
    curl -sf "$BASE/healthz" >/dev/null && break
    sleep 0.1
done
curl -s "$BASE/healthz"; echo

say "batch 1 (last-write-wins: bob ends at 201; dave present-empty)"
R1=$(curl -s -X POST "$BASE/v1/batches" \
    -H 'content-type: application/json' \
    --data @examples/input/batch1.json)
echo "$R1" | tee "$OUT/batch1.json"
V1=$(echo "$R1" | python3 -c 'import sys,json;print(json.load(sys.stdin)["version"])')
ROOT1=$(echo "$R1" | python3 -c 'import sys,json;print(json.load(sys.stdin)["root_hex"])')

say "batch 2 (delete carol, add frank)"
R2=$(curl -s -X POST "$BASE/v1/batches" \
    -H 'content-type: application/json' \
    --data @examples/input/batch2.json)
echo "$R2" | tee "$OUT/batch2.json"
ROOT2=$(echo "$R2" | python3 -c 'import sys,json;print(json.load(sys.stdin)["root_hex"])')

get_proof() { # <version> <ascii-key>
    local v=$1 k=$2
    local hex
    hex=$(python3 -c 'import sys;print(sys.argv[1].encode().hex())' "$k")
    curl -s -X POST "$BASE/v1/proofs" -H 'content-type: application/json' \
        -d "{\"version\": $v, \"key_hex\": \"$hex\"}"
}

ascii_hex() { python3 -c 'import sys;print(sys.argv[1].encode().hex())' "$1"; }

verify_both() { # <file> <root> <key-ascii> <expect-present|expect-absent>
    local f=$1 root=$2 key=$3 expect=$4
    local kh; kh=$(ascii_hex "$key")
    local out_r out_p
    out_r=$(check_rust "$f" "$root" "$kh")
    out_p=$(check_py   "$f" "$root" "$kh")
    echo "  rust verifier:   $out_r"
    echo "  python verifier: $out_p"
    case $expect in
        present) [[ $out_r == VERIFIED\ PRESENT* && $out_p == VERIFIED\ PRESENT* ]] ;;
        absent)  [[ $out_r == VERIFIED\ ABSENT*  && $out_p == VERIFIED\ ABSENT*  ]] ;;
    esac
}

say "existence proof at v1: bob = 201 (Rust + Python independent verifiers)"
get_proof "$V1" "bob" > "$OUT/p-bob-v1.json"; cat "$OUT/p-bob-v1.json" | python3 -m json.tool | head -20
verify_both "$OUT/p-bob-v1.json" "$ROOT1" "bob" present

say "non-existence (interior) at v1: bz between bob and carol"
get_proof "$V1" "bz" > "$OUT/p-bz-v1.json"
python3 -m json.tool "$OUT/p-bz-v1.json" | grep -E 'kind|index|key_hex'
verify_both "$OUT/p-bz-v1.json" "$ROOT1" "bz" absent

say "non-existence (boundary) at v1: zz after last key"
get_proof "$V1" "zz" > "$OUT/p-zz-v1.json"
verify_both "$OUT/p-zz-v1.json" "$ROOT1" "zz" absent

say "historical: carol present at v1 but ABSENT at v2 after delete"
get_proof "$V1" "carol" > "$OUT/p-carol-v1.json"
verify_both "$OUT/p-carol-v1.json" "$ROOT1" "carol" present
get_proof 2 "carol" > "$OUT/p-carol-v2.json"
verify_both "$OUT/p-carol-v2.json" "$ROOT2" "carol" absent

say "tamper test: flip one sibling-hash byte -> both verifiers REJECT"
python3 - "$OUT/p-bob-v1.json" "$OUT/p-bob-tampered.json" <<'PY'
import json,sys
doc=json.load(open(sys.argv[1]))
for s in doc["path"]:
    if s["hash"]:
        b=bytearray.fromhex(s["hash"]); b[0]^=1; s["hash"]=b.hex(); break
json.dump(doc,open(sys.argv[2],"w"))
PY
if "${VERIFY[@]}" "$OUT/p-bob-tampered.json" "$ROOT1" "$(ascii_hex bob)" 2>/dev/null; then
    echo "  FAIL: Rust verifier accepted tampered proof"; exit 1
else
    echo "  Rust verifier REJECTED tampered proof (as required)"
fi
if python3 scripts/verify.py "$OUT/p-bob-tampered.json" "$ROOT1" "$(ascii_hex bob)" 2>/dev/null; then
    echo "  FAIL: Python verifier accepted tampered proof"; exit 1
else
    echo "  Python verifier REJECTED tampered proof (as required)"
fi

say "list historical roots (both remain queryable)"
curl -s "$BASE/v1/roots" | python3 -m json.tool

say "restart service (same data dir) — state and history must survive"
kill $PID; wait $PID 2>/dev/null || true
trap - EXIT
./target/release/merkle-proof-service --db "$DB" --listen "$ADDR" \
    >>"$OUT/server.log" 2>&1 &
PID=$!
trap 'kill $PID 2>/dev/null || true' EXIT
for _ in $(seq 1 50); do curl -sf "$BASE/healthz" >/dev/null && break; sleep 0.1; done
echo "current root after restart:"
curl -s "$BASE/v1/root" | python3 -m json.tool
get_proof 2 "frank" > "$OUT/p-frank-v2.json"
verify_both "$OUT/p-frank-v2.json" "$ROOT2" "frank" present
get_proof 1 "bob" > "$OUT/p-bob-v1b.json"
verify_both "$OUT/p-bob-v1b.json" "$ROOT1" "bob" present

echo
echo "ALL DEMO CHECKS PASSED"
