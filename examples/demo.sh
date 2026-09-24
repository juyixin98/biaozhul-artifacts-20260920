#!/usr/bin/env bash
# 制品分阶段晋级 —— 接口手工演示脚本(纯 curl,无 jq 依赖)。
# 用法:
#   1) 先启动服务: PORT=8080 cargo run --release
#   2) 另开终端执行: BASE=http://127.0.0.1:8080 ./examples/demo.sh
set -u
BASE="${BASE:-http://127.0.0.1:8080}"

say() { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
req() { # method url [data] [content-type]
  local m="$1" u="$2" data="${3:-}" ct="${4:-application/json}"
  if [ -n "$data" ]; then
    curl -sS -X "$m" "$BASE$u" -H "Content-Type: $ct" --data "$data" -w '\n[HTTP %{http_code}]\n'
  else
    curl -sS -X "$m" "$BASE$u" -w '\n[HTTP %{http_code}]\n'
  fi
}

say "健康检查"
req GET /health

say "1) 上传制品内容 v1(服务端计算 SHA-256,内容按摘要寻址、不可变)"
BODY=$(printf 'release-binary-v1')
DIGEST=$(curl -sS -X PUT "$BASE/artifacts/content" \
  -H 'Content-Type: application/octet-stream' --data-binary "$BODY" \
  | sed -E 's/.*"digest":"([0-9a-f]{64})".*/\1/')
echo "v1 digest = $DIGEST"

say "2) 登记制品 app-1,绑定该摘要,初始阶段 dev"
req POST /artifacts "{\"id\":\"app-1\",\"digest\":\"$DIGEST\"}"

say "3a) 无审批直接晋级 -> 409 APPROVAL_REQUIRED"
req POST /artifacts/app-1/promote "{\"digest\":\"$DIGEST\"}"

say "3b) 审批 verification,但没有测试证明 -> 409 PROOF_REQUIRED"
req POST /artifacts/app-1/approvals \
  "{\"approval_id\":\"ap-ver-1\",\"digest\":\"$DIGEST\",\"target_stage\":\"verification\",\"approver\":\"qa-lead\",\"decision\":\"approved\",\"comment\":\"ok\"}"
req POST /artifacts/app-1/promote "{\"digest\":\"$DIGEST\"}"

say "3c) 提交 passed 的 unit-test 证明(证明引用同一摘要)"
req POST /artifacts/app-1/proofs \
  "{\"proof_id\":\"proof-unit-1\",\"digest\":\"$DIGEST\",\"kind\":\"unit-test\",\"result\":\"passed\",\"detail\":\"42/42 cases\"}"

say "3d) 晋级 dev -> verification 成功"
req POST /artifacts/app-1/promote "{\"digest\":\"$DIGEST\"}"

say "4a) 进入 release 需要 release 审批 + unit-test 与 release-gate 证明"
req POST /artifacts/app-1/approvals \
  "{\"approval_id\":\"ap-rel-1\",\"digest\":\"$DIGEST\",\"target_stage\":\"release\",\"approver\":\"release-manager\",\"decision\":\"approved\"}"
say "4b) 缺 release-gate 证明 -> 409 PROOF_REQUIRED(不能发布)"
req POST /artifacts/app-1/promote "{\"digest\":\"$DIGEST\"}"
say "4c) 补上 release-gate 证明"
req POST /artifacts/app-1/proofs \
  "{\"proof_id\":\"proof-gate-1\",\"digest\":\"$DIGEST\",\"kind\":\"release-gate\",\"result\":\"passed\"}"
say "4d) 晋级 verification -> release 成功"
req POST /artifacts/app-1/promote "{\"digest\":\"$DIGEST\"}"

say "5) 审批后替换内容:用同一 id 绑定新摘要 -> 409 IMMUTABLE_DIGEST"
DIGEST2=$(printf 'release-binary-v2-tampered' | curl -sS -X PUT "$BASE/artifacts/content" \
  -H 'Content-Type: application/octet-stream' --data-binary @- \
  | sed -E 's/.*"digest":"([0-9a-f]{64})".*/\1/')
echo "v2 digest = $DIGEST2"
req POST /artifacts "{\"id\":\"app-1\",\"digest\":\"$DIGEST2\"}"
say "   拿新摘要去晋级 -> 409 DIGEST_MISMATCH"
req POST /artifacts/app-1/promote "{\"digest\":\"$DIGEST2\"}"

say "6) 重复晋级(已在 release 终态)-> 409 ALREADY_AT_FINAL_STAGE"
req POST /artifacts/app-1/promote "{\"digest\":\"$DIGEST\"}"

say "7) 回退 release -> dev(只追加历史,不重建产物)"
req POST /artifacts/app-1/rollback \
  "{\"digest\":\"$DIGEST\",\"target_stage\":\"dev\",\"reason\":\"incident #42\"}"

say "8) 查看制品全貌:阶段、历史、证明、审批"
req GET /artifacts/app-1

say "9) 门禁视图:回退后审批/证明仍在,可直接重新晋级"
req GET /artifacts/app-1/gates
req POST /artifacts/app-1/promote "{\"digest\":\"$DIGEST\"}"

say "10) 并发回退:16 个请求只有 1 个成功"
for i in $(seq 1 16); do
  curl -sS -X POST "$BASE/artifacts/app-1/rollback" \
    -H 'Content-Type: application/json' \
    --data "{\"digest\":\"$DIGEST\",\"target_stage\":\"dev\"}" \
    -o /dev/null -w '%{http_code} ' &
done
wait
echo

say "11) 内容审计:按旧摘要取回的字节与最初一致(未被重建/替换)"
req GET "/artifacts/content/$DIGEST" application/octet-stream 2>/dev/null || true
curl -sS "$BASE/artifacts/content/$DIGEST"; echo
