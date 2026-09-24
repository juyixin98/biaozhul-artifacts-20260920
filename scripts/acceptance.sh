#!/usr/bin/env bash
# 一键验收：构建 -> 生成示例密钥/证据 -> 启动服务 -> 跑全部攻击/边界场景 -> 断言判定。
# 不连接任何真实集群或镜像仓库；全部摘要、签名、验签均真实执行。
set -uo pipefail

cd "$(dirname "$0")/.."
PORT="${PORT:-18080}"
BASE="http://127.0.0.1:$PORT"
REPORTS_DIR="$(mktemp -d)"
trap 'kill "${SRV_PID:-0}" 2>/dev/null; rm -rf "$REPORTS_DIR"' EXIT

echo "==> 1/5 编译（使用已锁定的 vendor 依赖）"
go build -mod=vendor -o /tmp/admissiond ./cmd/admissiond || { echo "编译失败"; exit 1; }
go build -mod=vendor -o /tmp/mirrorctl ./cmd/mirrorctl || { echo "编译失败"; exit 1; }

echo "==> 2/5 生成示例（真实 ed25519 密钥 + 真实签名/验签）"
/tmp/mirrorctl gen-examples -out examples >/dev/null || { echo "示例生成失败"; exit 1; }

echo "==> 3/5 启动离线准入服务 :$PORT"
/tmp/admissiond -addr "127.0.0.1:$PORT" -trust examples/keys/trust.json \
  -reports "$REPORTS_DIR" >/tmp/admissiond-acceptance.log 2>&1 &
SRV_PID=$!
for _ in $(seq 1 50); do
  curl -sf "$BASE/healthz" >/dev/null 2>&1 && break
  sleep 0.1
done
curl -sf "$BASE/healthz" >/dev/null || { echo "服务未就绪，见 /tmp/admissiond-acceptance.log"; exit 1; }

echo "==> 4/5 场景断言"
R=examples/requests
fail=0
check() { # 期望退出码由 eval.py --expect 给出
  if python3 scripts/eval.py "$@"; then :; else fail=$((fail+1)); fi
}
check "$PORT" "合规镜像(证据齐全) -> ALLOW"            "$R/good-image.json" --sbom "$R/good-sbom.json" --verification "$R/good-verification.json" --expect ALLOW
check "$PORT" "合规镜像但缺验签结果 -> UNKNOWN"         "$R/good-image.json" --sbom "$R/good-sbom.json" --expect UNKNOWN
check "$PORT" "完全缺证据(仅镜像) -> UNKNOWN"           "$R/good-image.json" --expect UNKNOWN
check "$PORT" "标签漂移(旧验签结果+新内容) -> DENY"     "$R/tag-drift-image.json" --sbom "$R/good-sbom.json" --verification "$R/tag-drift-verification.json" --expect DENY
check "$PORT" "root + 有效豁免 -> ALLOW(EXEMPT)"       "$R/root-image.json" --sbom "$R/good-sbom.json" --verification "$R/root-verification.json" --exemption "$R/root-exemption-valid.json" --expect ALLOW
check "$PORT" "root + 过期豁免 -> DENY"                "$R/root-image.json" --sbom "$R/good-sbom.json" --verification "$R/root-verification.json" --exemption "$R/root-exemption-expired.json" --expect DENY
check "$PORT" "豁免越界(豁免IMG-SIGNED) -> DENY"       "$R/root-image.json" --sbom "$R/good-sbom.json" --verification "$R/root-verification.json" --exemption "$R/overreach-exemption.json" --expect DENY
check "$PORT" "恶意缺字段(缺user/priv/base/sbom) -> UNKNOWN" "$R/malicious-missing-fields.json" --verification "$R/malicious-verification.json" --expect UNKNOWN
check "$PORT" "特权容器 privileged=true -> DENY"       "$R/privileged-image.json" --sbom "$R/privileged-sbom.json" --verification "$R/privileged-verification.json" --expect DENY

echo "==> 5/5 报告不可变检查（重评估不覆盖旧报告）"
n=$(curl -s "$BASE/v1/reports?limit=500" | jq '.count')
first=$(curl -s "$BASE/v1/reports?limit=500" | jq -r '.reports[-1].id')
# 对同一镜像再评估一次，必须生成新 ID，旧报告结论不变。
python3 scripts/eval.py "$PORT" "重评估合规镜像(应新增报告)" "$R/good-image.json" \
  --sbom "$R/good-sbom.json" --verification "$R/good-verification.json" >/dev/null
n2=$(curl -s "$BASE/v1/reports?limit=500" | jq '.count')
old=$(curl -s "$BASE/v1/reports/$first" | jq -r '.id')
if [[ "$n2" -le "$n" || "$old" != "$first" ]]; then
  echo "!! 报告不可变性被破坏：$n -> $n2，旧ID $first -> $old"
  fail=$((fail+1))
else
  echo "报告数量 $n -> $n2，旧报告 $first 原样保留"
fi

echo
if (( fail == 0 )); then
  echo "✅ 验收全部通过（$((n2)) 份报告，策略版本冻结）"
else
  echo "❌ 有 $fail 个场景未通过"
  exit 1
fi
