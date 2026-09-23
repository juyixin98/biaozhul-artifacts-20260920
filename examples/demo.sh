#!/usr/bin/env bash
# End-to-end happy-path demo against a running server:
# out-of-order parts, idempotent retransmit, repeated complete, restart recovery.
#
# Usage: ./examples/demo.sh [base_url]   (default http://127.0.0.1:8080)
set -euo pipefail

BASE="${1:-http://127.0.0.1:8080}"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
cd "$WORK"

say() { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
code() { curl -sS -o /tmp/demo.body -w '%{http_code}' "$@"; }

say "prepare 30-byte object (chunk_size=10)"
head -c 30 /dev/urandom > demo.bin
SIZE=$(stat -c%s demo.bin)
HASH=$(sha256sum demo.bin | cut -d' ' -f1)
echo "size=$SIZE sha256=$HASH"

say "create upload session"
RESP=$(curl -sS -X POST "$BASE/uploads" -H 'content-type: application/json' \
  -d "{\"key\":\"demo/demo.bin\",\"total_size\":$SIZE,\"sha256\":\"$HASH\",\"chunk_size\":10}")
echo "$RESP" | python3 -m json.tool
ID=$(echo "$RESP" | python3 -c 'import sys,json;print(json.load(sys.stdin)["upload_id"])')
echo "upload_id=$ID"

say "object is 404 before publish"
curl -sS -o /dev/null -w 'GET /objects/demo/demo.bin -> %{http_code}\n' "$BASE/objects/demo/demo.bin"

say "split + upload in order 2,0,1 (out of order)"
dd if=demo.bin of=p0 bs=1 skip=0  count=10 status=none
dd if=demo.bin of=p1 bs=1 skip=10 count=10 status=none
dd if=demo.bin of=p2 bs=1 skip=20 count=10 status=none
for n in 2 0 1; do
  curl -sS -X PUT "$BASE/uploads/$ID/parts/$n" --data-binary @p$n
  echo
done

say "idempotent retransmit of part 0 (same sha256, HTTP 200)"
curl -sS -X PUT "$BASE/uploads/$ID/parts/0" --data-binary @p0; echo

say "session status: nothing missing"
curl -sS "$BASE/uploads/$ID" | python3 -m json.tool

say "complete -> 201, then complete again (idempotent)"
curl -sS -X POST "$BASE/uploads/$ID/complete" | python3 -m json.tool
curl -sS -o /dev/null -w 'second complete -> %{http_code}\n' -X POST "$BASE/uploads/$ID/complete"

say "read back and compare"
curl -sS -D - "$BASE/objects/demo/demo.bin" -o got.bin | grep -iE 'HTTP/|x-object-sha256|etag'
cmp demo.bin got.bin && echo "OK: bytes identical"

say "restart server yourself (Ctrl-C and re-run), then this still works:"
echo "  curl -sS -X POST $BASE/uploads/$ID/complete   # idempotent 201"
echo "  curl -sS $BASE/objects/demo/demo.bin -o got2.bin && cmp demo.bin got2.bin"
