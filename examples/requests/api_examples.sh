#!/usr/bin/env bash
# dlang JSON 服务请求样例。
# 用法：
#   1) python -m dl.server --port 8000
#   2) bash examples/requests/api_examples.sh
#
# 依赖 curl。所有位置均为字符偏移、半开区间 [start, end)。
set -euo pipefail
BASE="${BASE:-http://127.0.0.1:8000}"

say() { printf '\n===== %s =====\n' "$1"; }

say "1. 健康检查"
curl -s "$BASE/health"

say "2. 一次性全量解析（合法程序）"
curl -s -X POST "$BASE/parse" \
  -H 'Content-Type: application/json' \
  -d '{"source":"var x = 1 + 2 * 3;\nfn f(a) { return a; }"}'

say "3. 全量解析（未闭合括号 -> 错误位置精确）"
curl -s -X POST "$BASE/parse" \
  -H 'Content-Type: application/json' \
  -d '{"source":"var x = (1 + 2;"}'

say "4. 字符串内符号不影响外层结构（附带 tokens 观察）"
curl -s -X POST "$BASE/parse" \
  -H 'Content-Type: application/json' \
  -d '{"source":"var s = \"(a; b){}\";","include_tokens":true}'

say "5. 创建增量文档"
DOC=$(curl -s -X POST "$BASE/documents" \
  -H 'Content-Type: application/json' \
  -d '{"source":"var a = 1;\nvar b = 2;\nvar c = 3;"}')
echo "$DOC"
ID=$(printf '%s' "$DOC" | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')

say "6. 在中间插入一条声明（观察 reuse_events：后面的声明被复用）"
curl -s -X POST "$BASE/documents/$ID/edits" \
  -H 'Content-Type: application/json' \
  -d '{"edits":[{"start":22,"end":22,"text":"var mid = 99;\n"}]}'

say "7. 连续两次编辑（顺序应用，每次都做增量解析）"
curl -s -X POST "$BASE/documents/$ID/edits" \
  -H 'Content-Type: application/json' \
  -d '{"edits":[{"start":0,"end":3,"text":"let"},{"start":0,"end":0,"text":"// edited\n"}]}'

say "8. 单编辑简写 + 删除（删掉 ' = 99' 一部分）"
curl -s -X POST "$BASE/documents/$ID/edits" \
  -H 'Content-Type: application/json' \
  -d '{"start":40,"end":46,"text":""}'

say "9. 查看当前文档"
curl -s "$BASE/documents/$ID"

say "10. 删除文档"
curl -s -X DELETE "$BASE/documents/$ID"

say "11. 错误请求（非法 JSON）"
curl -s -X POST "$BASE/parse" -d 'not-json' || true
echo
