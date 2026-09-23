#!/usr/bin/env bash
# 各端点的 curl 用法速查（假定服务运行在 localhost:8080）。
# 直接执行本脚本会逐条发送；也可只复制其中的命令手动执行。
set -euo pipefail
BASE=${BASE:-http://127.0.0.1:8080}
DIR=$(dirname "$0")/requests

echo "# 健康检查"
curl -sS "$BASE/health"; echo

echo "# 发送单条事件（eventTime 为 epoch 毫秒）"
curl -sS -X POST "$BASE/events" -H 'Content-Type: application/json' \
  --data @"$DIR/event.json"; echo

echo "# eventTime 也支持 ISO-8601 字符串"
curl -sS -X POST "$BASE/events" -H 'Content-Type: application/json' \
  --data @"$DIR/event-iso8601.json"; echo

echo "# 批量发送（同一批内重复也会被识别）"
curl -sS -X POST "$BASE/events/batch" -H 'Content-Type: application/json' \
  --data @"$DIR/batch.json"; echo

echo "# manual 模式：推进水位线到 15001（回退会返回 advanced=false）"
curl -sS -X POST "$BASE/watermark" -H 'Content-Type: application/json' \
  --data @"$DIR/watermark.json"; echo

echo "# bounded 模式：触发一次周期水位线推进"
curl -sS -X POST "$BASE/tick" -H 'Content-Type: application/json' \
  --data @"$DIR/tick.json"; echo

echo "# 拉取 seq>0 的已发射事件；drain=true 同时从缓冲区移除"
curl -sS "$BASE/outputs?sinceSeq=0&drain=false" | head -c 400; echo

echo "# 查看可观察计数（seen/accepted/suppressed/unguarded/payloadMismatch/...）"
curl -sS "$BASE/stats"; echo

echo "# 立即将状态快照落盘"
curl -sS -X POST "$BASE/checkpoint" -H 'Content-Type: application/json' -d '{}'; echo

echo "# 清空内存状态与快照文件（慎用）"
# curl -sS -X POST "$BASE/admin/reset" -H 'Content-Type: application/json' -d '{}'; echo
