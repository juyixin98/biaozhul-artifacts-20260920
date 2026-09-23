#!/usr/bin/env bash
# End-to-end demo:
#   1) start the Axum server, create main -> child -> grandchild, interleave writes
#   2) show snapshot contents, shared pages, and copy accounting
#   3) crash-process demo: arm a fault, let crash-driver die with exit 37, reopen + recover
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DATA="$(mktemp -d)"
trap 'kill ${SERVER_PID:-0} 2>/dev/null || true; rm -rf "$DATA" || true' EXIT
ADDR="127.0.0.1:18099"
BASE="http://$ADDR"
export COW_DATA_DIR="$DATA" COW_ADDR="$ADDR"

echo "== build =="
cargo build --quiet || exit 1
SERVER="$ROOT/target/debug/cow-server"
DRIVER="$ROOT/target/debug/crash-driver"

jget() { python3 -c 'import sys,json;x=json.load(sys.stdin);'"$1"; }

echo "== start server (data dir $DATA) =="
"$SERVER" &
SERVER_PID=$!
sleep 0.7
api() { curl -sS "$@"; }

echo; echo "== create main and write 5 pages =="
api -X POST "$BASE/branches" -H 'content-type: application/json' -d '{"name":"main"}' \
  | jget 'print("  ok root="+str(x["root"])+" copied_pages="+str(x["copied_pages"]))'
for i in 0 1 2 3 4; do
    b64=$(printf 'base-%s' "$i" | base64 | tr -d '\n')
    api -X PUT "$BASE/branches/main/pages/$i" -H 'content-type: application/json' \
        -d "{\"data_b64\":\"$b64\"}" >/dev/null
done

echo "== two-level branches =="
api -X POST "$BASE/branches" -H 'content-type: application/json' -d '{"name":"child","parent":"main"}' >/dev/null
api -X POST "$BASE/branches" -H 'content-type: application/json' -d '{"name":"grandchild","parent":"child"}' >/dev/null
api "$BASE/branches" | python3 -m json.tool

echo; echo "== interleaved overwrites =="
put() { # branch index text
    local b=$1 i=$2 t=$3
    local b64; b64=$(printf '%s' "$t" | base64 | tr -d '\n')
    api -X PUT "$BASE/branches/$b/pages/$i" -H 'content-type: application/json' \
        -d "{\"data_b64\":\"$b64\"}" >/dev/null
}
put main 1 'main@1'
put child 2 'child@2'
put grandchild 3 'gc@3'
put child 1 'child@1'
put main 0 'main@0'

echo "contents after divergence:"
for b in main child grandchild; do
    for i in 0 1 2 3; do
        api "$BASE/branches/$b/pages/$i" | python3 -c '
import sys, json, base64
b, i = sys.argv[1], sys.argv[2]
x = json.load(sys.stdin)
pid = x["page_id"]
data = base64.b64decode(x["data_b64"]).decode()
print("  %s[%s] pid=%s %s" % (b, i, pid, data))' "$b" "$i"
    done
done

echo; echo "== live pages / refcounts / copy accounting =="
api "$BASE/stats" | python3 -m json.tool

echo; echo "== delete child; shared pages survive =="
api -X DELETE "$BASE/branches/child" >/dev/null
echo "grandchild[2] still readable (it shared that page with the deleted child):"
api "$BASE/branches/grandchild/pages/2" \
  | jget 'import base64;print("  pid="+str(x["page_id"])+" "+base64.b64decode(x["data_b64"]).decode())'

kill $SERVER_PID 2>/dev/null; wait $SERVER_PID 2>/dev/null

echo; echo "== real crash demo via crash-driver (separate process, COW_CRASH_KILL=1) =="
rm -rf "$DATA"; export COW_CRASH_KILL=1
"$DRIVER" --dir "$DATA" init
"$DRIVER" --dir "$DATA" branch main >/dev/null
for i in 0 1 2; do "$DRIVER" --dir "$DATA" write main "$i" "base-$i" >/dev/null; done
"$DRIVER" --dir "$DATA" branch child main >/dev/null

"$DRIVER" --dir "$DATA" fault before_manifest
set +e
"$DRIVER" --dir "$DATA" write child 1 'crash-then-retry'
code=$?
set -e
echo "process exit code at crash point: $code (expected 37)"

echo "-- reopen runs recovery; child[1] must still hold the OLD committed value base-1 --"
"$DRIVER" --dir "$DATA" read child 1
"$DRIVER" --dir "$DATA" fault-clear >/dev/null
echo "-- retry with fault disarmed --"
"$DRIVER" --dir "$DATA" write child 1 'crash-then-retry' >/dev/null
"$DRIVER" --dir "$DATA" read child 1
echo; echo "demo OK"
