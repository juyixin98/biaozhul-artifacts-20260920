#!/usr/bin/env bash
# renfa JSON 服务请求样例。先在另一个终端启动服务：
#   python -m renfa.service --port 8080
set -u
HOST=${HOST:-127.0.0.1}
PORT=${PORT:-8080}
BASE="http://${HOST}:${PORT}"

echo "== 1. 健康检查 =="
curl -s "${BASE}/healthz"
echo; echo

echo "== 2. fullmatch（连接/选择/括号/有限重复）=="
curl -s -X POST "${BASE}/match" \
  -H 'Content-Type: application/json' \
  --data @examples/request_fullmatch.json
echo; echo

echo "== 3. findall（Unicode 码点，偏移也是码点）=="
curl -s -X POST "${BASE}/match" \
  -H 'Content-Type: application/json' \
  --data @examples/request_findall_unicode.json
echo; echo

echo "== 4. debug：返回 token（含源码位置）、AST、NFA =="
curl -s -X POST "${BASE}/match" \
  -H 'Content-Type: application/json' \
  --data @examples/request_debug.json
echo; echo

echo "== 5. 语法错误（带行列定位，HTTP 400）=="
curl -s -X POST "${BASE}/match" \
  -H 'Content-Type: application/json' \
  -d '{"pattern":"a(b|c","text":"abc"}'
echo
