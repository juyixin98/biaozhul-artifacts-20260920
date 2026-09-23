#!/usr/bin/env bash
# 端到端手动演示：启动服务 -> 发送乱序/重复事件 -> 推进水位线 -> 观察极迟重复标志
#              -> 关闭 -> 重启 -> 验证恢复。所有响应原样输出到终端。
#
# 用法: ./examples/demo.sh
set -euo pipefail
cd "$(dirname "$0")/.."

PORT=${PORT:-18080}
BASE="http://127.0.0.1:${PORT}"
STATE_DIR="build/demo-state"
JAR="build/dedup.jar"

[ -f "$JAR" ] || ./build.sh >/dev/null
rm -rf "$STATE_DIR"
mkdir -p "$STATE_DIR"

say() { printf '\n\033[1;36m==== %s ====\033[0m\n' "$1"; }
req() { # method path [body]
  local m=$1 p=$2 body=${3:-}
  if [ -n "$body" ]; then
    curl -sS -X "$m" "$BASE$p" -H 'Content-Type: application/json' -d "$body" | jq .
  else
    curl -sS -X "$m" "$BASE$p" | jq .
  fi
}

say "启动服务 (allowedLateness=5000ms, manual 水位线, 端口 $PORT)"
java -jar "$JAR" --port "$PORT" --allowed-lateness-ms 5000 \
     --watermark-mode manual --state-file "$STATE_DIR/snapshot.json" \
     > "$STATE_DIR.server.log" 2>&1 &
SERVER_PID=$!
trap 'kill $SERVER_PID 2>/dev/null || true' EXIT
for _ in $(seq 1 50); do curl -sS "$BASE/health" >/dev/null 2>&1 && break; sleep 0.1; done

say "1) 首次事件 evt-1 @10000（载荷 v=1） -> 期望 ACCEPT"
req POST /events '{"id":"evt-1","key":"user-a","eventTime":10000,"payload":{"v":1}}'

say "2) 同 ID 重复但载荷不同 v=2 -> 期望 SUPPRESS + payloadMismatch=true"
req POST /events '{"id":"evt-1","key":"user-a","eventTime":10000,"payload":{"v":2}}'

say "3) 乱序到达：更晚事件 evt-2 @12000"
req POST /events '{"id":"evt-2","key":"user-a","eventTime":12000,"payload":{}}'

say "4) evt-1 副本带着更晚的 eventTime=11000（乱序、在窗口内） -> 仍 SUPPRESS"
req POST /events '{"id":"evt-1","key":"user-a","eventTime":11000,"payload":{"v":1}}'

say "5) 推进水位线到 12000（承诺下界 7000，旧墓碑仍保留）"
req POST /watermark '{"watermark":12000}'

say "6) evt-1 @10000 仍在窗口内 -> SUPPRESS"
req POST /events '{"id":"evt-1","key":"user-a","eventTime":10000,"payload":{"v":1}}'

say "7) 时钟回退：尝试把水位线回退到 1 -> 期望 advanced=false"
req POST /watermark '{"watermark":1}'

say "8) 推进水位线到 15001（下界 10001）：evt-1 锚点已在步骤4延长到11000，仍存活"
req POST /watermark '{"watermark":15001}'

say "8b) evt-1 @11000 锚点仍在窗口内 -> SUPPRESS（墓碑未释放）"
req POST /events '{"id":"evt-1","key":"user-a","eventTime":11000,"payload":{"v":1}}'

say "8c) 注意：eventTime=10000 的极迟副本按其自身时间判定已在窗口外 -> UNGUARANTEED"
req POST /events '{"id":"evt-1","key":"user-a","eventTime":10000,"payload":{"v":1}}'

say "8d) 推进水位线到 16001（下界 11001 > 锚点11000），墓碑真正释放，timeEvicted+1"
req POST /watermark '{"watermark":16001}'

say "9) 此时 evt-1 @11000 再到 -> UNGUARANTEED + dedupGuaranteed=false（透传）"
req POST /events '{"id":"evt-1","key":"user-a","eventTime":11000,"payload":{"v":1}}'

say "10) 读取全部已发射输出（drain=false 不删除）"
req GET '/outputs?drain=false'

say "11) 统计计数"
req GET /stats

say "12) 优雅关闭（收到 SIGTERM 后落盘）"
kill -TERM $SERVER_PID
wait $SERVER_PID 2>/dev/null || true
trap - EXIT
echo "服务已停止，快照文件：$STATE_DIR/snapshot.json"
echo "--- 快照内容 ---"
jq . "$STATE_DIR/snapshot.json"

say "13) 重启服务（同一快照），evt-2@12000 的状态应延续"
java -jar "$JAR" --port "$PORT" --allowed-lateness-ms 5000 \
     --watermark-mode manual --state-file "$STATE_DIR/snapshot.json" \
     > "$STATE_DIR.server2.log" 2>&1 &
SERVER_PID=$!
trap 'kill $SERVER_PID 2>/dev/null || true' EXIT
for _ in $(seq 1 50); do curl -sS "$BASE/health" >/dev/null 2>&1 && break; sleep 0.1; done

say "14) 重启后统计：suppressed/unguarded 等计数延续"
req GET /stats

say "15) 重启后一条全新窗口内事件 evt-3 @20000 -> ACCEPT"
req POST /events '{"id":"evt-3","key":"user-a","eventTime":20000,"payload":{}}'

say "16) 清理"
req POST /admin/reset '{}'
kill -TERM $SERVER_PID 2>/dev/null || true
wait $SERVER_PID 2>/dev/null || true
trap - EXIT
echo "演示完成"
