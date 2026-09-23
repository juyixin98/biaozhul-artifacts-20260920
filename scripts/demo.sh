#!/usr/bin/env bash
# End-to-end demo: build a table over HTTP, exercise get/scan/validate,
# corrupt a copy on disk, and show the strict-validation failure.
#
# Requires: cargo, curl. Run from the repository root:
#   scripts/demo.sh
set -euo pipefail

ADDR="${ADDR:-127.0.0.1:8088}"
DIR="${DIR:-./tabledata-demo}"
NAME="demo"
BASE="http://${ADDR}"

wait_for() {
  for _ in $(seq 1 100); do
    if curl -fsS "${BASE}/healthz" >/dev/null 2>&1; then return 0; fi
    sleep 0.1
  done
  echo "server did not become ready" >&2
  exit 1
}

echo "==> building release binary"
cargo build --release

echo "==> starting server (dir=$DIR addr=$ADDR)"
rm -rf "$DIR"
./target/release/psst serve --dir "$DIR" --addr "$ADDR" >/tmp/psst-demo.log 2>&1 &
SRV=$!
trap 'kill "$SRV" 2>/dev/null || true' EXIT
wait_for
echo "    server up (log: /tmp/psst-demo.log)"

echo
echo "==> health"
curl -fsS "${BASE}/healthz"; echo

echo
echo "==> create table '$NAME' from examples/http/sample_table.json"
curl -fsS -X PUT "${BASE}/tables/${NAME}" \
  -H 'Content-Type: application/json' \
  --data-binary @examples/http/sample_table.json; echo

echo
echo "==> duplicate create -> expect 409"
set +e
curl -s -o /dev/null -w "HTTP %{http_code}\n" -X PUT "${BASE}/tables/${NAME}" \
  -H 'Content-Type: application/json' \
  --data-binary @examples/http/sample_table.json
set -e

echo
echo "==> point get: empty key"
curl -fsS "${BASE}/tables/${NAME}/get?key="; echo

echo
echo "==> point get: events/.../sequence-0007"
K=$(python3 -c 'print((b"events/2026-09-23/tenant-0042/sequence-0007").hex())')
curl -fsS "${BASE}/tables/${NAME}/get?key=${K}"; echo

echo
echo "==> point miss"
curl -fsS "${BASE}/tables/${NAME}/get?key=ffff"; echo

echo
echo "==> scan first 5"
curl -fsS "${BASE}/tables/${NAME}/scan?limit=5"; echo

echo
echo "==> validate (strict, whole file)"
curl -fsS -X POST "${BASE}/tables/${NAME}/validate" | python3 -m json.tool

echo
echo "==> corrupt the on-disk file by flipping one byte"
FILE="${DIR}/${NAME}"
python3 - "$FILE" <<'PY'
import sys
p = sys.argv[1]
b = bytearray(open(p,'rb').read())
b[len(b)//2] ^= 0xFF
open(p,'wb').write(b)
print("flipped byte at offset", len(b)//2)
PY

echo "==> validate again -> expect HTTP 400 + corruption"
set +e
curl -s -w "\nHTTP %{http_code}\n" -X POST "${BASE}/tables/${NAME}/validate"
set -e

echo
echo "==> delete and list"
curl -fsS -X DELETE "${BASE}/tables/${NAME}"; echo
curl -fsS "${BASE}/tables"; echo

echo
echo "demo finished OK"
