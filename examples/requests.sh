#!/usr/bin/env bash
# 请求样例：对一个 manual-clock、窗口长度 100 的全新服务演示完整流程。
# 用法：先启动全新服务  ./run-server.sh   再运行  ./examples/requests.sh
# （服务进程内存中保存窗口状态；重复运行本脚本请重启服务，或用 BASE 指向新实例）
set -u
BASE=${BASE:-http://localhost:8080}
say() { printf '\n### %s\n' "$1"; }

say "健康检查"
curl -s "$BASE/health"; echo

say "提交单个事件 {timestamp:100, value:5}"
curl -s -X POST "$BASE/events" -H 'Content-Type: application/json' \
  -d '{"timestamp":100,"value":5}'; echo

say "批量提交（含同时间戳、负值、全重复）"
curl -s -X POST "$BASE/events" -H 'Content-Type: application/json' \
  -d '{"events":[{"timestamp":100,"value":7},{"timestamp":100,"value":7},
                 {"timestamp":95,"value":-10},{"timestamp":120,"value":100}]}'; echo

say "查询中位数（窗口 (0,100]：-10,5,7,7 → median 6.0；ts=120 为未来事件）"
curl -s "$BASE/median"; echo

say "查询 q=0.25 / q=0.75（线性插值）"
curl -s "$BASE/quantile?q=0.25"; echo
curl -s "$BASE/quantile?q=0.75"; echo

say "推进时钟到 196（cutoff=96，ts=95 的 -10 过期；ts=120 的 100 已进入窗口）"
curl -s -X POST "$BASE/advance" -H 'Content-Type: application/json' -d '{"now":196}'; echo

say "再次查询中位数（窗口 (96,196]：5,7,7,100 → median 7.0，count=4）"
curl -s "$BASE/median"; echo

say "提交迟到事件（ts=10，必被丢弃）"
curl -s -X POST "$BASE/events" -H 'Content-Type: application/json' \
  -d '{"timestamp":10,"value":999}'; echo

say "状态"
curl -s "$BASE/stats"; echo

say "推进到 221（cutoff=121，ts=120 也过期）后窗口为空"
curl -s -X POST "$BASE/advance" -H 'Content-Type: application/json' -d '{"now":221}'; echo
curl -s "$BASE/median"; echo
