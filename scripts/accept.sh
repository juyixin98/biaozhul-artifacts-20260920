#!/usr/bin/env bash
# 一键本地验收：安装依赖检查 -> 生成示例 -> 单元测试 -> 启动真实服务 -> HTTP 验收。
# 用法：bash scripts/accept.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PORT="${PORT:-8000}"
BASE="http://127.0.0.1:${PORT}"
LOG="$(mktemp -t groundseg-uvicorn.XXXXXX.log)"

echo "== 1/5 环境与依赖检查 =="
python3 -c "import numpy, fastapi, uvicorn, httpx, pydantic; print('依赖就绪')"

echo "== 2/5 生成示例输入与真值 =="
PYTHONPATH=src python3 scripts/generate_examples.py

echo "== 3/5 库内精确率/召回率 =="
PYTHONPATH=src python3 scripts/run_metrics.py

echo "== 4/5 自动化测试 (pytest) =="
python3 -m pytest tests/ -q

cleanup() {
  if [[ -n "${SERVER_PID:-}" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
  rm -f "$LOG"
}
trap cleanup EXIT

echo "== 5/5 启动真实 uvicorn 服务并做 HTTP 验收 =="
# 打开 HMAC 签名（真实密码学操作），验收脚本会独立复算并验签
GROUNDSEG_HMAC_KEY="acceptance-secret-2026" \
  PYTHONPATH=src python3 -m uvicorn groundseg.app:app \
  --host 127.0.0.1 --port "$PORT" >"$LOG" 2>&1 &
SERVER_PID=$!

# 等待服务就绪（最多 ~20 秒），就绪后立即继续，不做固定 sleep
for _ in $(seq 1 100); do
  if curl -sf "$BASE/healthz" >/dev/null 2>&1; then
    echo "服务已就绪 (pid=$SERVER_PID)"
    break
  fi
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "服务启动失败，日志：" >&2
    cat "$LOG" >&2
    exit 1
  fi
  sleep 0.2
done

GROUNDSEG_HMAC_KEY="acceptance-secret-2026" \
  python3 scripts/acceptance_http.py "$BASE"

echo
echo "全部验收步骤完成 ✅"
