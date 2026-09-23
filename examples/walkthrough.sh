#!/usr/bin/env bash
# 最小手工走查：用 curl + python3 完成一次无序通道的 send/recv/ack 与一次超时拒绝。
# 所有检查点签名与 Merkle 证明均从运行中的服务实时取得（没有任何预置假数据）。
#
# 用法：
#   IBC_TEACH_DB=/tmp/ibc_walk.db .venv/bin/uvicorn ibc_teach.app:app --port 8000 &
#   bash examples/walkthrough.sh
set -euo pipefail

BASE=${BASE:-http://127.0.0.1:8000}
PY=${PYTHON:-python3}
j() { "$PY" -c 'import sys,json;d=json.load(sys.stdin);print(eval(sys.argv[1]))' "$1"; }

echo "== health =="
curl -s "$BASE/health"; echo

echo "== create unordered channel =="
curl -s -X POST "$BASE/channels" -H 'Content-Type: application/json' \
  -d @examples/payloads/02_create_unordered_channel.json; echo

echo "== send packet (timeout_height=2) =="
STATUS=$(curl -s -o /tmp/send.json -w "%{http_code}" -X POST "$BASE/packets/send" -H 'Content-Type: application/json' -d '{
  "src_chain":"chain-a","src_port":"port-a","src_channel":"channel-0",
  "timeout_height":2,"timeout_time_ns":0,"data_hex":"cafe","amount":25}')
SEND=$(cat /tmp/send.json)
echo "$SEND"
if [ "$STATUS" != "201" ]; then echo "FAIL: expected 201, got $STATUS"; exit 1; fi
SEQ=$(echo "$SEND" | j 'd["sequence"]')

echo "== finalize chain-a (承诺入块，产生签名检查点) =="
curl -s -X POST "$BASE/chains/chain-a/blocks" -H 'Content-Type: application/json' -d '{}'; echo

echo "== 查询承诺键并取成员证明 =="
PATHHEX=$(curl -s "$BASE/keys?kind=commitment&port=port-a&channel=channel-0&sequence=$SEQ" | j 'd["path"]')
PROOF=$(curl -s "$BASE/chains/chain-a/proof?height=1&path=$PATHHEX")

echo "== 目的链推进到高度 2（== timeout_height），接收必须 408 =="
curl -s -X POST "$BASE/chains/chain-b/blocks" -H 'Content-Type: application/json' -d '{}' >/dev/null
curl -s -X POST "$BASE/chains/chain-b/blocks" -H 'Content-Type: application/json' -d '{}' >/dev/null
BODY=$(echo "$PROOF" | "$PY" -c '
import sys,json
pr=json.load(sys.stdin)
print(json.dumps({"packet":{
 "src_chain":"chain-a","src_port":"port-a","src_channel":"channel-0",
 "dst_chain":"chain-b","dst_port":"port-b","dst_channel":"channel-0",
 "sequence":'$SEQ',"timeout_height":2,"timeout_time_ns":0,
 "data_hex":"cafe","amount":25},
 "checkpoint":pr["checkpoint"],"proof":pr["proof"]}))')
STATUS=$(curl -s -o /tmp/recv.json -w "%{http_code}" \
  -X POST "$BASE/packets/recv" -H 'Content-Type: application/json' -d "$BODY")
cat /tmp/recv.json; echo
if [ "$STATUS" != "408" ]; then echo "FAIL: expected 408, got $STATUS"; exit 1; fi

echo "== 用回执缺失的非成员证明做超时退款 =="
RKEY=$(curl -s "$BASE/keys?kind=receipt&port=port-b&channel=channel-0&sequence=$SEQ" | j 'd["path"]')
RPROOF=$(curl -s "$BASE/chains/chain-b/proof?height=2&path=$RKEY")
TBODY=$(echo "$RPROOF" | "$PY" -c '
import sys,json
pr=json.load(sys.stdin)
assert pr["exists"] is False
print(json.dumps({"packet":{
 "src_chain":"chain-a","src_port":"port-a","src_channel":"channel-0",
 "dst_chain":"chain-b","dst_port":"port-b","dst_channel":"channel-0",
 "sequence":'$SEQ',"timeout_height":2,"timeout_time_ns":0,
 "data_hex":"cafe","amount":25},
 "checkpoint":pr["checkpoint"],"proof":pr["proof"]}))')
STATUS=$(curl -s -o /tmp/timeout.json -w "%{http_code}" \
  -X POST "$BASE/packets/timeout" -H 'Content-Type: application/json' -d "$TBODY")
cat /tmp/timeout.json; echo
if [ "$STATUS" != "200" ]; then echo "FAIL: expected 200, got $STATUS"; exit 1; fi

echo "== 包终态 =="
curl -s "$BASE/packets" | "$PY" -m json.tool
