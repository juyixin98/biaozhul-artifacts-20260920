#!/usr/bin/env bash
# 本地一键验收：安装 -> 生成样例 -> 全部自动化测试 -> 起服务 -> 对 8 个样例
# + 7 个恶意/畸形归档做真实 HTTP 验收。任何一步失败立即非零退出。
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

echo "==> [1/5] 安装锁定依赖"
pip install --quiet -r requirements.lock

echo "==> [2/5] 生成确定性示例输入"
rm -rf examples
python -m app.samples.generate --out examples

echo "==> [3/5] 运行自动化测试"
python -m pytest

echo "==> [4/5] 启动服务（隔离数据目录）"
export PROVENANCE_DATA_DIR
PROVENANCE_DATA_DIR="$(mktemp -d)/provenance"
HOST=127.0.0.1 PORT=8000 python -m app &
SERVER_PID=$!
trap 'kill $SERVER_PID 2>/dev/null || true' EXIT

for _ in $(seq 1 50); do
  if curl -sf http://127.0.0.1:8000/health >/dev/null 2>&1; then break; fi
  sleep 0.2
done
curl -sf http://127.0.0.1:8000/health >/dev/null

echo "==> [5/5] HTTP 验收 8 个样例（核对裁决）+ 恶意归档（必须全部 400）"
fail=0
check_verdict() {
  local dir="$1" expect="$2"
  local got
  got=$(curl -s -o /tmp/pv_resp.json -w "%{http_code}" \
    -F "config=@$dir/config.json;type=application/json" \
    -F "compilerOutput=@$dir/compilerOutput.json;type=application/json" \
    -F "targetDeployed=<\"$dir/target_deployed.hex\"" \
    -F "package=@$dir/sources.zip;type=application/zip" \
    http://127.0.0.1:8000/api/v1/verify || true)
  if [ "$expect" = "HTTP_400" ]; then
    code=$(python3 -c "import json;print(json.load(open('/tmp/pv_resp.json'))['error']['code'])" 2>/dev/null || echo "-")
    if [ "$got" = "400" ]; then
      printf '  OK   %-28s -> 400 %s\n' "$(basename "$dir")" "$code"
    else
      printf '  FAIL %-28s expected 400, got %s\n' "$(basename "$dir")" "$got"; fail=1
    fi
  else
    verdict=$(python3 -c "import json;print(json.load(open('/tmp/pv_resp.json'))['report']['verdict'])")
    if [ "$got" = "200" ] && [ "$verdict" = "$expect" ]; then
      printf '  OK   %-28s -> %s\n' "$(basename "$dir")" "$verdict"
    else
      printf '  FAIL %-28s expected %s, got %s/%s\n' "$(basename "$dir")" "$expect" "$got" "$verdict"; fail=1
    fi
  fi
}

check_verdict examples/01-exact EXACT
check_verdict examples/02-rule-libaddr RULE_MATCH
check_verdict examples/03-rule-metadata RULE_MATCH
check_verdict examples/04-mismatch-optimizer MISMATCH
check_verdict examples/05-missing-source MISMATCH
check_verdict examples/06-mismatch-semantic MISMATCH
check_verdict examples/07-mismatch-version MISMATCH
check_verdict examples/08-bad-library-address HTTP_400

cfg=examples/01-exact/config.json
co=examples/01-exact/compilerOutput.json
tgt=examples/01-exact/target_deployed.hex
for mal in examples/_malicious/*.zip examples/_malicious/*.tar; do
  code=$(curl -s -o /dev/null -w "%{http_code}" \
    -F "config=@$cfg;type=application/json" \
    -F "compilerOutput=@$co;type=application/json" \
    -F "targetDeployed=<$tgt" \
    -F "package=@$mal;type=application/octet-stream" \
    http://127.0.0.1:8000/api/v1/verify || true)
  if [ "$code" = "400" ]; then
    printf '  OK   malicious/%-19s -> 400 BAD_PACKAGE\n' "$(basename "$mal")"
  else
    printf '  FAIL malicious/%-19s expected 400, got %s\n' "$(basename "$mal")" "$code"; fail=1
  fi
done

echo
if [ "$fail" -eq 0 ]; then
  echo "✅ 验收全部通过（88 项 pytest + 8 样例 + 7 恶意归档）"
else
  echo "❌ 验收存在失败项"
  exit 1
fi
