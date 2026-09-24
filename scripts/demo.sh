#!/usr/bin/env bash
# 端到端示例：通过 HTTP 接口演示检查点写/查/合并/未来块拒绝。
# 前置：Anvil 在 $RPC_URL，合约已部署且地址在 $CONTRACT_ADDRESS。
set -u
API="${API_URL:-http://127.0.0.1:8001}"
RPC="${RPC_URL:-http://127.0.0.1:8547}"

rpc() { curl -s -X POST "$RPC" -H 'Content-Type: application/json' -d "$1"; }
mine() { rpc "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"evm_mine\",\"params\":[{\"blocks\":$1}]}"; }
jget() { python3 -c "import sys,json;d=json.load(sys.stdin);print($1)"; }

echo "== 1. 空记录查询 =="
curl -s "$API/checkpoints/0" | python3 -m json.tool

echo "== 2. 写入 value=10（交易在下一块打包）=="
curl -s -X POST "$API/checkpoints" -H 'Content-Type: application/json' -d '{"value":10}' | python3 -m json.tool

echo "== 3. 挖 4 个空隙块后写入 value=20 =="
mine 4 >/dev/null
curl -s -X POST "$API/checkpoints" -H 'Content-Type: application/json' -d '{"value":20}' | python3 -m json.tool

echo "== 4. 历史检查点 =="
curl -s "$API/history" | python3 -m json.tool

echo "== 5. 查询：第一值块、空隙中、当前块 =="
CUR=$(rpc '{"jsonrpc":"2.0","id":1,"method":"eth_blockNumber","params":[]}' | jget 'int(d["result"],16)')
for B in 0 1 2 3 4 5 "$CUR"; do
  printf "block %s -> " "$B"
  curl -s "$API/checkpoints/$B" | jget '"value="+str(d["value"])+" found="+str(d["found"])'
done

echo "== 6. 未来块查询被拒绝（HTTP 400） =="
FUT=$((CUR + 3))
curl -s -w "\nHTTP %{http_code}\n" "$API/checkpoints/$FUT"

echo "== 7. 最新检查点 =="
curl -s "$API/latest" | python3 -m json.tool
