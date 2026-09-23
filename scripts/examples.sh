#!/usr/bin/env bash
# 手动请求样例（curl）。用法：
#   scripts/examples.sh            # 默认 http://127.0.0.1:8080
#   BASE=http://127.0.0.1:28123 scripts/examples.sh
set -euo pipefail
BASE="${BASE:-http://127.0.0.1:8080}"

say() { echo; echo "### $*"; }

say "健康检查（注意 scope 字段：保证只覆盖内置本地文件接收器）"
curl -s "$BASE/health"; echo

say "追加 3 条输入（POST /inputs，body 为 JSON，text 字段会被分词计数）"
curl -s -X POST "$BASE/inputs" -H 'Content-Type: application/json' -d '{"text":"a b a"}'; echo
curl -s -X POST "$BASE/inputs" -H 'Content-Type: application/json' -d '{"text":"b c"}'; echo
curl -s -X POST "$BASE/inputs" -H 'Content-Type: application/json' -d '{"text":"c c c"}'; echo

say "处理一个微批（默认 1 条 = 一个接收器事务）"
curl -s -X POST "$BASE/process" -H 'Content-Type: application/json' -d '{"maxRecords":1}'; echo

say "再处理 10 条（把剩余输入一次处理完）"
curl -s -X POST "$BASE/process" -H 'Content-Type: application/json' -d '{"maxRecords":10}'; echo

say "查看一致性快照视图（GET /state）"
curl -s "$BASE/state"; echo

say "查看接收器【可见输出】（GET /outputs，每行 JSON，带 inputOffset）"
curl -s "$BASE/outputs"; echo

say "查看接收器提交标记（GET /markers）"
curl -s "$BASE/markers"; echo

say "故障注入样例：在下个微批的“提交之后、快照提升之前”硬终止进程"
echo "  curl -X POST $BASE/process -d '{\"maxRecords\":1,\"failPoint\":\"AFTER_COMMIT\"}'"
echo "  进程会以退出码 17 立即退出；重新启动服务（scripts/run.sh）后自动恢复"
echo "  可选 failPoint：AFTER_PROCESS / AFTER_STATE_PERSIST / AFTER_OUTPUT_PREPARE / AFTER_COMMIT"

say "重启后显式再执行一次恢复对账（幂等，可安全重复调用）"
curl -s -X POST "$BASE/recover"; echo
