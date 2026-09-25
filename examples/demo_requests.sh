#!/usr/bin/env bash
# 端到端请求演示：启动服务 -> 轮换 -> 交错加解密 -> 错误版本拒绝 -> 销毁 -> 审计核验
# 用法: bash examples/demo_requests.sh [PORT]
# 依赖: curl, jq
set -euo pipefail

PORT="${1:-8080}"
BASE="http://127.0.0.1:${PORT}"
DATA_DIR="$(mktemp -d -t kva-demo-XXXXXX)"

echo "== 数据目录: ${DATA_DIR}"
python3 -m keyversion --data-dir "${DATA_DIR}" --port "${PORT}" serve >/tmp/kva-demo-server.log 2>&1 &
SERVER_PID=$!
trap 'kill ${SERVER_PID} 2>/dev/null || true' EXIT

# 等待服务就绪
for _ in $(seq 1 50); do
  curl -sf "${BASE}/health" >/dev/null 2>&1 && break
  sleep 0.1
done

req() { # method path [json body]
  local method="$1" path="$2" body="${3:-}"
  if [[ -n "${body}" ]]; then
    curl -sS -X "${method}" -H 'Content-Type: application/json' -d "${body}" "${BASE}${path}"
  else
    curl -sS -X "${method}" "${BASE}${path}"
  fi
}

echo; echo "== 1) 健康检查"
req GET /health | jq .

echo; echo "== 2) 尚无 active 版本时加密 -> 预期 409 no_active_version"
req POST /encrypt '{"plaintext":"hello"}' | jq .

echo; echo "== 3) 首次轮换，产生 v1(active)"
V1_JSON=$(req POST /keys/rotate)
echo "${V1_JSON}" | jq .
V1=$(echo "${V1_JSON}" | jq -r .version.version_id)

echo; echo "== 4) 用 v1 加密"
CT1_JSON=$(req POST /encrypt '{"plaintext":"first secret"}')
echo "${CT1_JSON}" | jq .
CT1=$(echo "${CT1_JSON}" | jq -r .ciphertext)

echo; echo "== 5) 轮换到 v2；v1 -> retired"
V2=$(req POST /keys/rotate | jq -r .version.version_id)
echo "v1=${V1} v2=${V2}"

echo; echo "== 6) 交错请求：新明文用 v2 加密"
CT2_JSON=$(req POST /encrypt '{"plaintext":"second secret"}')
echo "${CT2_JSON}" | jq .
CT2=$(echo "${CT2_JSON}" | jq -r .ciphertext)

echo; echo "== 7) 错误版本拒绝：显式指定 retired 的 v1 加密 -> 409"
req POST /encrypt "{\"plaintext\":\"x\",\"version_id\":\"${V1}\"}" | jq .

echo; echo "== 8) 解密按历史版本：v1 的旧密文仍可解（retired）"
req POST /decrypt "{\"ciphertext\":\"${CT1}\"}" | jq .
echo "    v2 的新密文："
req POST /decrypt "{\"ciphertext\":\"${CT2}\"}" | jq .

echo; echo "== 9) 篡改密文一比特 -> 400 invalid_envelope"
BADCT=$(python3 -c "import base64,sys; b=bytearray(base64.b64decode('${CT2}')); b[-1]^=1; print(base64.b64encode(b).decode())")
req POST /decrypt "{\"ciphertext\":\"${BADCT}\"}" | jq .

echo; echo "== 10) 销毁 v1 -> 旧密文不可恢复"
req POST "/keys/${V1}/destroy" | jq '.version | {version_id,state,has_material}'
req POST /decrypt "{\"ciphertext\":\"${CT1}\"}" | jq .

echo; echo "== 11) 版本清单"
req GET /keys | jq .

echo; echo "== 12) 审计日志（不应出现明文/密钥材料）"
req GET /audit | jq '.count, (.entries[] | {seq,action,version_id,result,detail})'

echo; echo "== 审计文件落盘字节中的秘密扫描（应无输出）："
grep -aE 'first secret|second secret' "${DATA_DIR}/audit.log" && echo "!! 发现泄露" || echo "OK: 审计文件无明文"
echo
echo "演示完成，数据目录保留在: ${DATA_DIR}"
trap - EXIT
kill ${SERVER_PID} 2>/dev/null || true
