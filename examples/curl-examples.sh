#!/usr/bin/env bash
# JSON-RPC 2.0 网关请求样例。先启动服务：
#   ./jsonrpc-gateway -addr :8080
set -u
BASE="${BASE:-http://127.0.0.1:8080}"

say() { printf '\n### %s\n' "$1"; }

say "1) 普通请求（字符串 ID，位置参数）"
curl -s -i -X POST "$BASE" -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"subtract","params":[42,17],"id":"req-1"}'

say "2) 普通请求（数字 ID，命名参数）——数字 ID 原样保留，不与 \"2\" 混淆"
curl -s -X POST "$BASE" -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"subtract","params":{"a":42,"b":17},"id":2}'
echo

say "3) 通知（无 id）——HTTP 204，无响应体"
curl -s -i -X POST "$BASE" -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"ping"}'

say "4) 批处理：合法请求 + 通知 + 非法项混合（通知不产生响应）"
curl -s -X POST "$BASE" -H 'Content-Type: application/json' \
  -d '[
    {"jsonrpc":"2.0","method":"sum","params":[1,2,3],"id":1},
    {"jsonrpc":"2.0","method":"ping"},
    {"jsonrpc":"2.0","method":"nope","id":"x"},
    {"foo":"bar"},
    {"jsonrpc":"2.0","method":"ping","id":"y"}
  ]'
echo

say "5) 全通知批次——HTTP 204"
curl -s -i -X POST "$BASE" -H 'Content-Type: application/json' \
  -d '[{"jsonrpc":"2.0","method":"ping"},{"jsonrpc":"2.0","method":"sum","params":[1]}]'

say "6) 空数组——单个 Invalid Request 错误对象（不是空数组）"
curl -s -X POST "$BASE" -H 'Content-Type: application/json' -d '[]'
echo

say "7) 解析错误 -32700（JSON 本身损坏）"
curl -s -X POST "$BASE" -H 'Content-Type: application/json' -d '{"jsonrpc":'
echo

say "8) 无效请求 -32600（JSON 合法但不符合协议）"
curl -s -X POST "$BASE" -H 'Content-Type: application/json' -d '{"foo":1}'
echo

say "9) 乱序完成：三个 delay 并发执行，按完成顺序返回，按 id 对应"
curl -s -X POST "$BASE" -H 'Content-Type: application/json' \
  -d '[
    {"jsonrpc":"2.0","method":"delay","params":[300],"id":"A"},
    {"jsonrpc":"2.0","method":"delay","params":[100],"id":"B"},
    {"jsonrpc":"2.0","method":"delay","params":[200],"id":"C"}
  ]'
echo

say "10) 超大整数 ID 原样回显（不走 float64）"
curl -s -X POST "$BASE" -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"ping","id":9007199254740993}'
echo

say "11) 非 POST 方法——HTTP 405"
curl -s -i "$BASE"
