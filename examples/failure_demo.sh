#!/usr/bin/env bash
# Failure-path demo: missing parts (422), different content for same part (409),
# wrong whole-object hash (422 + stays unreadable), immutability (409).
#
# Usage: ./examples/failure_demo.sh [base_url]   (default http://127.0.0.1:8080)
set -euo pipefail

BASE="${1:-http://127.0.0.1:8080}"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
cd "$WORK"

say() { printf '\n\033[1;33m== %s\033[0m\n' "$*"; }
show() { # show <expected_code> <curl args...>
  local want="$1"; shift
  local got
  got=$(curl -sS -o body -w '%{http_code}' "$@")
  echo "HTTP $got (expected $want)"; cat body; echo
  test "$got" = "$want"
}

say "missing parts -> 422 on complete"
head -c 20 /dev/urandom > f.bin
H=$(sha256sum f.bin | cut -d' ' -f1)
R=$(curl -sS -X POST "$BASE/uploads" -H 'content-type: application/json' \
  -d "{\"key\":\"fail/missing\",\"total_size\":20,\"sha256\":\"$H\",\"chunk_size\":10}")
ID=$(echo "$R" | python3 -c 'import sys,json;print(json.load(sys.stdin)["upload_id"])')
dd if=f.bin of=p0 bs=1 count=10 status=none
curl -sS -X PUT "$BASE/uploads/$ID/parts/0" --data-binary @p0 >/dev/null
show 422 -X POST "$BASE/uploads/$ID/complete"
echo "missing_parts per status:"
curl -sS "$BASE/uploads/$ID" | python3 -c 'import sys,json;print(json.load(sys.stdin)["missing_parts"])'

say "same part number, different content -> 409 part_conflict"
R=$(curl -sS -X POST "$BASE/uploads" -H 'content-type: application/json' \
  -d "{\"key\":\"fail/conflict\",\"total_size\":6,\"sha256\":\"$H\",\"chunk_size\":3}")
ID2=$(echo "$R" | python3 -c 'import sys,json;print(json.load(sys.stdin)["upload_id"])')
printf 'aaa' | curl -sS -X PUT "$BASE/uploads/$ID2/parts/0" --data-binary @- >/dev/null
printf 'bbb' | show 409 -X PUT "$BASE/uploads/$ID2/parts/0" --data-binary @-

say "declared whole-object hash wrong -> 422 and object stays 404"
head -c 12 /dev/urandom > g.bin
BADHASH=$(printf '00%.0s' {1..32})
R=$(curl -sS -X POST "$BASE/uploads" -H 'content-type: application/json' \
  -d "{\"key\":\"fail/badhash\",\"total_size\":12,\"sha256\":\"$BADHASH\",\"chunk_size\":12}")
ID3=$(echo "$R" | python3 -c 'import sys,json;print(json.load(sys.stdin)["upload_id"])')
curl -sS -X PUT "$BASE/uploads/$ID3/parts/0" --data-binary @g.bin >/dev/null
show 422 -X POST "$BASE/uploads/$ID3/complete"
show 404 "$BASE/objects/fail/badhash"

say "immutability: publish an object, then recreating same key -> 409"
OKH=$(sha256sum g.bin | cut -d' ' -f1)
R=$(curl -sS -X POST "$BASE/uploads" -H 'content-type: application/json' \
  -d "{\"key\":\"fail/once\",\"total_size\":12,\"sha256\":\"$OKH\",\"chunk_size\":12}")
ID4=$(echo "$R" | python3 -c 'import sys,json;print(json.load(sys.stdin)["upload_id"])')
curl -sS -X PUT "$BASE/uploads/$ID4/parts/0" --data-binary @g.bin >/dev/null
curl -sS -X POST "$BASE/uploads/$ID4/complete" >/dev/null
show 409 -X POST "$BASE/uploads" -H 'content-type: application/json' \
  -d "{\"key\":\"fail/once\",\"total_size\":12,\"sha256\":\"$OKH\"}"

echo
echo "ALL FAILURE-PATH CHECKS PASSED"
