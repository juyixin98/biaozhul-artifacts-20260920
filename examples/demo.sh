#!/usr/bin/env bash
# 制品分阶段晋级 —— curl 端到端演示。
# 用法：
#   ./target/release/artifact-promotion        # 另开终端启动服务
#   bash examples/demo.sh [BASE_URL]
set -u

BASE="${1:-http://127.0.0.1:8080}"
pp() { if command -v python3 >/dev/null; then python3 -m json.tool; else cat; fi; }

echo "== 健康检查 =="
curl -s "$BASE/health"; echo

echo "== 1. 注册制品（摘要服务端计算）=="
REG=$(curl -s -X POST "$BASE/artifacts" \
  -H 'content-type: application/octet-stream' \
  --data-binary 'release-bundle-v1.2.3')
echo "$REG" | pp
ID=$(echo "$REG" | sed -n 's/.*"id":"\([^"]*\)".*/\1/p')
DIG=$(echo "$REG" | sed -n 's/.*"digest":"\([^"]*\)".*/\1/p')
echo "id=$ID  digest=$DIG"
echo "独立核对: $(printf 'release-bundle-v1.2.3' | sha256sum | cut -d' ' -f1)"

echo "== 2. 反例：证明引用错误摘要 -> 409 =="
curl -s -w ' [HTTP %{http_code}]\n' -X POST "$BASE/artifacts/$ID/proofs" \
  -H 'content-type: application/json' \
  -d "{\"kind\":\"unit-test\",\"digest\":\"sha256:$(printf 'evil' | sha256sum | cut -d' ' -f1)\",\"passed\":true}"

echo "== 3. 登记三条引用绑定摘要的通过证明 =="
for K in unit-test integration-test security-scan; do
  curl -s -o /dev/null -w "proof $K -> %{http_code}\n" -X POST "$BASE/artifacts/$ID/proofs" \
    -H 'content-type: application/json' \
    -d "{\"kind\":\"$K\",\"digest\":\"$DIG\",\"passed\":true,\"detail\":\"$K passed\"}"
done

echo "== 4. development -> validation =="
curl -s -o /dev/null -w 'promote -> %{http_code}\n' -X POST "$BASE/artifacts/$ID/promote" \
  -H 'content-type: application/json' -d '{}'

echo "== 5. 反例：未审批发布 -> 422 =="
curl -s -w ' [HTTP %{http_code}]\n' -X POST "$BASE/artifacts/$ID/promote" \
  -H 'content-type: application/json' -d '{"approved":false}'

echo "== 6. 审批后发布 validation -> release =="
curl -s -w ' [HTTP %{http_code}]\n' -X POST "$BASE/artifacts/$ID/promote" \
  -H 'content-type: application/json' -d '{"approved":true,"approver":"alice"}' | pp 2>/dev/null || true

echo "== 7. 反例：重复晋级 -> 409 =="
curl -s -w ' [HTTP %{http_code}]\n' -X POST "$BASE/artifacts/$ID/promote" \
  -H 'content-type: application/json' -d '{"approved":true,"approver":"alice"}'

echo "== 8. 反例：审批后替换内容 -> 409 =="
curl -s -w ' [HTTP %{http_code}]\n' -X POST "$BASE/artifacts/$ID/content/immutable" \
  -H 'content-type: application/octet-stream' --data-binary 'tampered-bytes'

echo "== 9. 回退 release -> validation（保留历史、不重建）=="
curl -s -o /dev/null -w 'rollback -> %{http_code}\n' -X POST "$BASE/artifacts/$ID/rollback" \
  -H 'content-type: application/json' -d '{"reason":"hotfix regression"}'
curl -s "$BASE/artifacts/$ID/history" | pp

echo "== 10. 内容与摘要不变，build_count 不增加 =="
curl -s -D - "$BASE/artifacts/$ID/content" -o /tmp/demo-content.bin | grep -i 'x-content-digest'
echo "content bytes: $(cat /tmp/demo-content.bin)"
curl -s "$BASE/artifacts/$ID" | pp | grep -E 'stage|build_count|digest'

echo "== 11. 并发回退 validation->development（10 个请求，恰 1 个成功）=="
for i in $(seq 1 10); do
  curl -s -o /dev/null -w '%{http_code}\n' -X POST "$BASE/artifacts/$ID/rollback" \
    -H 'content-type: application/json' -d "{\"reason\":\"race-$i\"}" &
done | sort | uniq -c
wait
echo "最终阶段: $(curl -s "$BASE/artifacts/$ID" | sed -n 's/.*"stage":"\([^"]*\)".*/\1/p')"
