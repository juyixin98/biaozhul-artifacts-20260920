#!/usr/bin/env bash
# 端到端手动演示：提案 -> 乱序+重复签名 -> 调度 -> 延时 -> 执行一次 -> 再执行被拒 -> 签名人变更
set -u
BASE=http://127.0.0.1:8000
RPC=http://127.0.0.1:8545
S1=0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266
S2=0x70997970C51812dc3A010C7d01b50e0d17dc79C8
S3=0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC
S4=0x90F79bf6EB2c4f870365E785982E1f101E93b906

j() { python3 -m json.tool; }
rpc() { curl -s -X POST $RPC -H 'Content-Type: application/json' \
  --data "{\"jsonrpc\":\"2.0\",\"method\":\"$1\",\"params\":$2,\"id\":1}"; }

echo "================ 0. /state ================"
curl -s $BASE/state | j

echo "================ 1. 提案 increment ================"
OP=$(curl -s -X POST $BASE/actions/increment -H 'Content-Type: application/json' -d '{}')
echo "$OP" | j
HASH=$(echo "$OP" | python3 -c 'import sys,json;print(json.load(sys.stdin)["op_hash"])')
NONCE=$(echo "$OP" | python3 -c 'import sys,json;print(json.load(sys.stdin)["nonce"])')

echo "================ 2a. 签名：S3 先签 ================"
curl -s -X POST $BASE/operations/$HASH/sign -H 'Content-Type: application/json' \
  -d "{\"signer\":\"$S3\",\"nonce\":$NONCE,\"validUntil\":0}" | j
echo "================ 2b. S3 重复再签（去重，collected 不变）================"
curl -s -X POST $BASE/operations/$HASH/sign -H 'Content-Type: application/json' \
  -d "{\"signer\":\"$S3\",\"nonce\":$NONCE,\"validUntil\":0}" | j
echo "================ 2c. S1 补签（乱序凑齐阈值 2）================"
curl -s -X POST $BASE/operations/$HASH/sign -H 'Content-Type: application/json' \
  -d "{\"signer\":\"$S1\",\"nonce\":$NONCE,\"validUntil\":0}" | j

echo "================ 3. approve 上链（只提交 S3 一份 + 重复 S3 也试试）================"
curl -s -X POST $BASE/operations/$HASH/approve -H 'Content-Type: application/json' \
  -d "{\"nonce\":$NONCE,\"validUntil\":0,\"signers\":[\"$S3\",\"$S3\"]}" | j
echo "================ 3b. 再 approve 交 S3+S1 -> 触发调度 ================"
curl -s -X POST $BASE/operations/$HASH/approve -H 'Content-Type: application/json' \
  -d "{\"nonce\":$NONCE,\"validUntil\":0}" | j

echo "================ 4. 延时未到 execute -> TimelockNotReady ================"
curl -s -X POST $BASE/operations/$HASH/execute -H 'Content-Type: application/json' \
  -d "{\"nonce\":$NONCE,\"validUntil\":0}" | j

echo "================ 5. 链上时间 +3s ================"
rpc evm_increaseTime '[3]' >/dev/null; rpc evm_mine '[]' >/dev/null; echo done

echo "================ 6. execute -> executed, counter=1 ================"
curl -s -X POST $BASE/operations/$HASH/execute -H 'Content-Type: application/json' \
  -d "{\"nonce\":$NONCE,\"validUntil\":0}" | j

echo "================ 7. 再 execute -> AlreadyExecuted ================"
curl -s -X POST $BASE/operations/$HASH/execute -H 'Content-Type: application/json' \
  -d "{\"nonce\":$NONCE,\"validUntil\":0}" | j

echo "================ 8. 签名人变更提案：[S1,S2,S4] 阈值2 ================"
COP=$(curl -s -X POST $BASE/signers/change -H 'Content-Type: application/json' \
  -d "{\"signers\":[\"$S1\",\"$S2\",\"$S4\"],\"threshold\":2}")
echo "$COP" | j
CHASH=$(echo "$COP" | python3 -c 'import sys,json;print(json.load(sys.stdin)["op_hash"])')
CNONCE=$(echo "$COP" | python3 -c 'import sys,json;print(json.load(sys.stdin)["nonce"])')
for S in $S2 $S3; do
  curl -s -X POST $BASE/operations/$CHASH/sign -H 'Content-Type: application/json' \
    -d "{\"signer\":\"$S\",\"nonce\":$CNONCE,\"validUntil\":0}" >/dev/null
done
curl -s -X POST $BASE/operations/$CHASH/approve -H 'Content-Type: application/json' \
  -d "{\"nonce\":$CNONCE,\"validUntil\":0}" | python3 -c 'import sys,json;d=json.load(sys.stdin);print("approve:",d["tx"]["status"],"scheduled:",d["onchain"]["scheduled"])'

echo "================ 9. 延时未到执行变更 ================"
curl -s -X POST $BASE/operations/$CHASH/execute -H 'Content-Type: application/json' \
  -d "{\"nonce\":$CNONCE,\"validUntil\":0}" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d["tx"]["status"], "|", d["tx"].get("error"))'

echo "================ 10. +3s 后执行变更 ================"
rpc evm_increaseTime '[3]' >/dev/null; rpc evm_mine '[]' >/dev/null
curl -s -X POST $BASE/operations/$CHASH/execute -H 'Content-Type: application/json' \
  -d "{\"nonce\":$CNONCE,\"validUntil\":0}" | python3 -c 'import sys,json;d=json.load(sys.stdin);print("outcome:",d["outcome"])'

echo "================ 11. 变更后 /state（S3 应消失，S4 在列）================"
curl -s $BASE/state | python3 -c 'import sys,json;d=json.load(sys.stdin);print("signers:",d["signers"]);print("threshold:",d["onchain_threshold"])'
