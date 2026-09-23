#!/usr/bin/env bash
# 端到端演示：启动本地目标服务 + SOCKS5 代理，跑一组 curl 样例，最后清理。
set -euo pipefail
cd "$(dirname "$0")/.."

BIN=/tmp/socks5d-demo
TARGET_PORT=9000

go build -o "$BIN" ./cmd/socks5d

python3 -m http.server "$TARGET_PORT" --bind 127.0.0.1 >/tmp/demo-target.log 2>&1 &
TARGET_PID=$!
"$BIN" -user demo -pass secret -allow-ports "$TARGET_PORT" >/tmp/demo-socks5d.log 2>&1 &
PROXY_PID=$!
trap 'kill $TARGET_PID $PROXY_PID 2>/dev/null || true' EXIT
sleep 1

echo '== GET /healthz ==';        curl -s http://127.0.0.1:8080/healthz
echo '== GET /whitelist ==';      curl -s http://127.0.0.1:8080/whitelist; echo
echo '== CONNECT IPv4 via proxy =='
curl -s --socks5 demo:secret@127.0.0.1:1080 "http://127.0.0.1:$TARGET_PORT/" -o /dev/null -w 'HTTP %{http_code}\n'
echo '== CONNECT domain via proxy =='
curl -s --socks5-hostname demo:secret@127.0.0.1:1080 "http://localhost:$TARGET_PORT/" -o /dev/null -w 'HTTP %{http_code}\n'
echo '== wrong password (expect curl exit 97) =='
curl -s --socks5 demo:wrong@127.0.0.1:1080 "http://127.0.0.1:$TARGET_PORT/" -o /dev/null || echo "curl exit=$?"
echo '== non-whitelisted host (expect curl exit 97) =='
curl -s --max-time 5 --socks5 demo:secret@127.0.0.1:1080 "http://8.8.8.8:$TARGET_PORT/" -o /dev/null || echo "curl exit=$?"
echo '== GET /stats ==';          curl -s http://127.0.0.1:8080/stats; echo
echo '== proxy log ==';           cat /tmp/demo-socks5d.log
