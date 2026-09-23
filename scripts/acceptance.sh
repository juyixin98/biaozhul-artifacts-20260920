#!/usr/bin/env bash
# 本地一键验收：创建 venv（首次）、安装锁定依赖、起 uvicorn、跑验收脚本、自动停服务。
set -euo pipefail
cd "$(dirname "$0")/.."

PORT="${PORT:-8000}"
export DRAIN_URL="http://127.0.0.1:${PORT}"

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install --quiet -r requirements.txt

uvicorn app.main:app --host 127.0.0.1 --port "${PORT}" >/tmp/drain-uvicorn.log 2>&1 &
SERVER_PID=$!
cleanup() { kill "${SERVER_PID}" >/dev/null 2>&1 || true; }
trap cleanup EXIT

# 等待 /healthz
for _ in $(seq 1 50); do
  if curl -fsS "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1; then break; fi
  sleep 0.2
done

echo "== 自动化测试（pytest）=="
python -m pytest -q

echo
echo "== 端到端验收（真实 HTTP + Ed25519）=="
python scripts/acceptance.py
