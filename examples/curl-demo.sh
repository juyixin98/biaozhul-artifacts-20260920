#!/usr/bin/env bash
# 端到端接口示例：自动启动服务（端口 0=系统分配空闲端口）→ 调用全部接口 → 关闭服务。
# 用法：./examples/curl-demo.sh
set -euo pipefail
cd "$(dirname "$0")/.."

BASE_DIR="$(pwd)"
JAVA_BIN="${JAVA_HOME:+$JAVA_HOME/bin/}java"
LOG="$(mktemp -t interval-demo.XXXXXX.log)"

[ -d build/classes ] || ./build.sh >/dev/null

echo ">> 启动服务（由系统分配空闲端口）..."
"$JAVA_BIN" -cp build/classes intervalindex.HttpServerApp 0 127.0.0.1 >"$LOG" 2>&1 &
SERVER_PID=$!
trap 'kill "$SERVER_PID" 2>/dev/null || true; rm -f "$LOG"' EXIT

# 从启动日志解析实际端口；若进程提前退出则报错
PORT=""
for _ in $(seq 1 100); do
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "!! 服务启动失败，日志：" >&2
    cat "$LOG" >&2
    exit 1
  fi
  PORT=$(sed -n 's#.*http://127\.0\.0\.1:\([0-9][0-9]*\).*#\1#p' "$LOG" | head -1)
  [ -n "$PORT" ] && break
  sleep 0.1
done
[ -n "$PORT" ] || { echo "!! 未能解析监听端口"; cat "$LOG"; exit 1; }
BASE="http://127.0.0.1:${PORT}"
echo ">> 服务已启动：$BASE (pid=$SERVER_PID)"

say() { echo; echo "== $* =="; }

say "健康检查"
curl -s "$BASE/health"; echo

say "插入 [1,5)"
curl -s -X POST "$BASE/intervals" -H 'Content-Type: application/json' -d '{"lo":1,"hi":5}'; echo

say "插入 3 份重复区间 [2,8)"
curl -s -X POST "$BASE/intervals" -H 'Content-Type: application/json' -d '{"lo":2,"hi":8,"count":3}'; echo

say "插入外层 [0,10) 与相邻的 [10,20)"
curl -s -X POST "$BASE/intervals" -H 'Content-Type: application/json' -d '{"lo":0,"hi":10}' >/dev/null
curl -s -X POST "$BASE/intervals" -H 'Content-Type: application/json' -d '{"lo":10,"hi":20}' >/dev/null
echo "done"

say "列出全部区间"
curl -s "$BASE/intervals"; echo

say "交集查询 [5,11)"
curl -s -X POST "$BASE/intervals/overlap" -H 'Content-Type: application/json' -d '{"lo":5,"hi":11}'; echo

say "交集查询 [8,10)（半开：[2,8) 不命中）"
curl -s -X POST "$BASE/intervals/overlap" -H 'Content-Type: application/json' -d '{"lo":8,"hi":10}'; echo

say "覆盖计数 t=0,2,5,8,10,20"
for t in 0 2 5 8 10 20; do
  printf 't=%-2s -> ' "$t"
  curl -s "$BASE/intervals/coverage?t=$t"; echo
done

say "删除 2 份 [2,8)"
curl -s -X DELETE "$BASE/intervals" -H 'Content-Type: application/json' -d '{"lo":2,"hi":8,"count":2}'; echo

say "query-string 删除剩余 [2,8)"
curl -s -X DELETE "$BASE/intervals?lo=2&hi=8&count=10"; echo

say "删除不存在的区间（预期 404）"
curl -s -w " [HTTP %{http_code}]\n" -X DELETE "$BASE/intervals" \
  -H 'Content-Type: application/json' -d '{"lo":100,"hi":200}'

say "空区间（预期 400）"
curl -s -w " [HTTP %{http_code}]\n" -X POST "$BASE/intervals" \
  -H 'Content-Type: application/json' -d '{"lo":3,"hi":3}'

say "逆序区间（预期 400）"
curl -s -w " [HTTP %{http_code}]\n" -X POST "$BASE/intervals" \
  -H 'Content-Type: application/json' -d '{"lo":9,"hi":2}'

echo; echo ">> 示例完成，关闭服务。"
