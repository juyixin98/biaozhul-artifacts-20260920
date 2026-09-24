#!/usr/bin/env bash
# 端到端演示：追加日志 -> 签名检查点 -> 导出 -> 验证 -> 篡改检测
# 前提：服务已在 $BASE（默认 http://127.0.0.1:8000）运行
set -euo pipefail
BASE="${BASE:-http://127.0.0.1:8000}"
command -v jq >/dev/null || { echo "需要 jq"; exit 1; }

echo "== 1. 追加 5 条审计记录 =="
for i in 1 2 3 4 5; do
  curl -sS -X POST "$BASE/entries" -H 'Content-Type: application/json' \
    -d "{\"actor\":\"alice\",\"action\":\"invoice.approve\",\"payload\":{\"invoice_id\":\"INV-10$i\"}}" \
    | jq -c '{seq: .entry.seq, hash: .entry_hash}'
done

echo "== 2. 创建签名检查点（验证者把它固定为可信锚）=="
CP=$(curl -sS -X POST "$BASE/checkpoints")
echo "$CP" | jq .
TRUSTED_CP=$(echo "$CP" | jq -c '.checkpoint')
TRUSTED_SIG=$(echo "$CP" | jq -r '.signature')

echo "== 3. 验证者带外获取公钥（信任锚；演示中从 /public_key 取）=="
PUB=$(curl -sS "$BASE/public_key" | jq -r '.public_key_pem')

echo "== 4. 导出完整日志并验证（应为 VALID）=="
EXPORT=$(curl -sS "$BASE/export")
jq -n --argjson e "$(echo "$EXPORT" | jq '.entries')" \
      --arg pub "$PUB" \
      --argjson cp "$TRUSTED_CP" --arg sig "$TRUSTED_SIG" \
  '{entries: $e, public_key_pem: $pub, trusted_checkpoint: $cp, trusted_signature: $sig}' \
  | curl -sS -X POST "$BASE/verify" -H 'Content-Type: application/json' -d @- | jq .

echo "== 5. 篡改第 2 条再验证（应为 TAMPERED）=="
TAMPERED=$(echo "$EXPORT" | jq '.entries[1].entry.payload.invoice_id = "INV-9999" | .entries')
jq -n --argjson e "$TAMPERED" --arg pub "$PUB" \
  '{entries: $e, public_key_pem: $pub}' \
  | curl -sS -X POST "$BASE/verify" -H 'Content-Type: application/json' -d @- | jq .

echo "== 6. 截尾（只交前 3 条）并用可信锚验证（应为 TRUNCATED）=="
TRUNC=$(echo "$EXPORT" | jq '.entries[:3]')
jq -n --argjson e "$TRUNC" --arg pub "$PUB" \
      --argjson cp "$TRUSTED_CP" --arg sig "$TRUSTED_SIG" \
  '{entries: $e, public_key_pem: $pub, trusted_checkpoint: $cp, trusted_signature: $sig}' \
  | curl -sS -X POST "$BASE/verify" -H 'Content-Type: application/json' -d @- | jq .
