#!/usr/bin/env bash
# 一键端到端演示：生成密钥 -> 后台启动服务 -> 跑 client demo -> 停服务。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
cd "$ROOT"

KEYS="$(mktemp -t anti-replay-keys.XXXXXX.json)"
PYTHON="${PYTHON:-python3}"
# 默认选取一个随机空闲端口，避免与残留调试进程冲突；可用 PORT=xxxx 覆盖。
if [[ -z "${PORT:-}" ]]; then
  PORT="$("$PYTHON" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')"
fi
BASE_URL="http://127.0.0.1:${PORT}"

cleanup() {
  if [[ -n "${SERVER_PID:-}" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
  rm -f "$KEYS"
}
trap cleanup EXIT

export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

echo "== 1/3 生成本地测试密钥 =="
"$PYTHON" scripts/keygen.py --keystore "$KEYS" --key-id demo-key-1

echo
echo "== 2/3 启动服务（:$PORT，内存 nonce 存储）=="
"$PYTHON" -m anti_replay.server --keystore "$KEYS" --port "$PORT" &
SERVER_PID=$!

# 等待端口就绪（最多约 5 秒）。
for _ in $(seq 1 50); do
  if "$PYTHON" - "$BASE_URL" <<'PY' 2>/dev/null
import sys, http.client
from urllib.parse import urlsplit
u = urlsplit(sys.argv[1])
c = http.client.HTTPConnection(u.hostname, u.port, timeout=1)
c.request("GET", "/health"); r = c.getresponse(); r.read()
sys.exit(0 if r.status == 200 else 1)
PY
  then
    break
  fi
  sleep 0.1
done

echo
echo "== 3/3 运行攻击场景演示 =="
"$PYTHON" examples/client.py demo --base-url "$BASE_URL" --keystore "$KEYS" "$@"
