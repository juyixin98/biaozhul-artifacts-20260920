#!/usr/bin/env bash
# 一组可直接执行的请求样例。先启动服务： scripts/run-server.sh
# 用法： scripts/requests.sh [baseUrl]
set -euo pipefail
BASE="${1:-http://127.0.0.1:8080}"

say() { printf '\n### %s\n' "$1"; }

say "词典版本列表"
curl -s "$BASE/api/dictionaries"

say "最小代价分词（v1）：研究生生命 -> 研究/生/生命"
curl -s -X POST "$BASE/api/segment" -H 'Content-Type: application/json' \
  --data-binary @samples/segment-best.json

say "N 最佳（k=5，重叠词长句）"
curl -s -X POST "$BASE/api/segment" -H 'Content-Type: application/json' \
  --data-binary @samples/segment-nbest.json

say "字典版本切换（v2）：同一句 -> 研究生/生命"
curl -s -X POST "$BASE/api/segment" -H 'Content-Type: application/json' \
  --data-binary @samples/segment-version-v2.json

say "含未知字符 X"
curl -s -X POST "$BASE/api/segment" -H 'Content-Type: application/json' \
  --data-binary @samples/segment-unknown.json

say "空串"
curl -s -X POST "$BASE/api/segment" -H 'Content-Type: application/json' \
  --data-binary @samples/segment-empty.json

say "小句穷举分词对照（DP vs 穷举）"
curl -s -X POST "$BASE/api/crosscheck" -H 'Content-Type: application/json' \
  --data-binary @samples/crosscheck.json

say "错误样例：词典版本不存在（404）"
curl -s -i -X POST "$BASE/api/segment" -H 'Content-Type: application/json' \
  -d '{"dictionary":"nope","text":"命"}'

say "错误样例：k 越界（400）"
curl -s -i -X POST "$BASE/api/segment" -H 'Content-Type: application/json' \
  -d '{"dictionary":"v1","text":"命","k":0}'
