#!/usr/bin/env bash
# HTTP 冒烟验收：在同一脚本生命周期内启动服务、发真实 HTTP 请求、制造崩溃、恢复、关闭。
set -uo pipefail
cd "$(dirname "$0")"
P="${1:-19137}"
rm -rf data/http-demo
java -cp build com.example.cptx.Main serve --dir data/http-demo --port "$P" --every 5 >logs/server.log 2>&1 &
SRV=$!
cleanup() { kill "$SRV" 2>/dev/null; }
trap cleanup EXIT

# 等待端口就绪
for i in $(seq 1 40); do
  curl -sf "localhost:$P/healthz" >/dev/null 2>&1 && break
  sleep 0.25
done

echo "== 1) health =="
curl -s "localhost:$P/healthz"; echo

echo "== 2) POST /events（5 条，触发检查点1）=="
curl -s -X POST "localhost:$P/events" -H 'Content-Type: application/json' \
  -d @examples/request-post-events.json \
  | jq -c '{accepted,firstOffset,lastOffset,cp:.status.lastCheckpointId,off:.status.nextOffset}'

echo "== 3) 再发 7 条（共 12 条，已有 2 个检查点）=="
curl -s -X POST "localhost:$P/events" -H 'Content-Type: application/json' \
  -d "$(jq -n '{events:[range(7)|{key:"A",value:1.0}]}')" \
  | jq -c '{accepted,cp:.status.lastCheckpointId,off:.status.nextOffset}'

echo "== 4) 注入 OUTPUT_COMMIT 故障于检查点 3，再发 5 条（12->17，触发检查点3）=="
curl -s -X POST "localhost:$P/faults" -H 'Content-Type: application/json' \
  -d '{"phase":"OUTPUT_COMMIT","checkpointId":3}' | jq -c .
curl -s -X POST "localhost:$P/events" -H 'Content-Type: application/json' \
  -d "$(jq -n '{events:[range(5)|{key:"B",value:0.5}]}')" | jq -c .

echo "== 5) 崩溃后 health 与写入拒绝（503）=="
curl -s "localhost:$P/healthz"; echo
curl -s -o /tmp/blocked.json -w 'HTTP %{http_code}\n' -X POST "localhost:$P/events" \
  -H 'Content-Type: application/json' -d '{"events":[{"key":"Z","value":1}]}'
jq -c . /tmp/blocked.json

echo "== 6) POST /recover 恢复（新进程语义，重放输入日志）=="
curl -s -X POST "localhost:$P/recover" -d '{}' \
  | jq -c '{recovered,resumedToOffset,cp:.status.lastCheckpointId,off:.status.nextOffset,Zpresent:(.status.summary.Z!=null)}'

echo "== 7) 恢复后最终 summary =="
curl -s "localhost:$P/summary" | jq -S .

echo "== 8) 非法输入返回 400 =="
curl -s -o /dev/null -w 'empty events -> HTTP %{http_code}\n' -X POST "localhost:$P/events" \
  -H 'Content-Type: application/json' -d '{"events":[]}'
curl -s -o /dev/null -w 'bad json    -> HTTP %{http_code}\n' -X POST "localhost:$P/events" \
  -H 'Content-Type: application/json' -d 'not-json'

cleanup() { :; }
