#!/usr/bin/env bash
# 端到端确定性演示：在临时端口启动服务，用 curl 执行一个固定序列，
#  覆盖 乱序 / 窗口边界 / 多分区全局水位 / 空闲恢复 / 容忍期晚到 / 侧输出 / 重复事件。
# 用法: ./scripts/demo.sh [端口]
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -n "${JAVA_HOME:-}" ] && [ -x "$JAVA_HOME/bin/java" ]; then
  JAVA="$JAVA_HOME/bin/java"
else
  JAVA="$(command -v java)"
fi
[ -d out/tumbling ] || ./scripts/compile.sh

PORT="${1:-18080}"
BASE="http://localhost:$PORT"

"$JAVA" -cp out tumbling.Main "$PORT" 10 2 > /tmp/tumbling-demo.log 2>&1 &
SERVER_PID=$!
trap 'kill "$SERVER_PID" 2>/dev/null || true' EXIT

# 等待端口就绪
for _ in $(seq 1 50); do
  curl -sf "$BASE/health" >/dev/null 2>&1 && break
  sleep 0.1
done

req() {
  local title="$1"; shift
  echo
  echo "────────────────────────────────────────────────────────────"
  echo "# $title"
  echo "\$ $*"
  echo
  "$@"
}

echo "================ 事件时间滚动窗口 — 确定性演示 ================"
echo "窗口=10  迟到容忍期=2  端口=$PORT"

req "0. 健康检查" curl -s "$BASE/health"

req "1. 批量乱序接入分区 a 的事件 t=2,7,5（均属 [0,10)）" \
  curl -s -X POST "$BASE/events" -H 'Content-Type: application/json' \
  -d '{"events":[{"partition":"a","eventId":"e2","eventTime":2},{"partition":"a","eventId":"e7","eventTime":7},{"partition":"a","eventId":"e5","eventTime":5}]}'

req "2. 分区 a 水位 → 10：[0,10) 到达窗口末端，触发 fire（计数 3，尚未 final）" \
  curl -s -X POST "$BASE/watermarks" -H 'Content-Type: application/json' \
  -d '{"partition":"a","watermark":10}'

req "3. 容忍期内晚到事件 t=6：产生 update（计数修订为 4，revision=2）" \
  curl -s -X POST "$BASE/events" -H 'Content-Type: application/json' \
  -d '{"partition":"a","eventId":"e6","eventTime":6}'

req "4. 水位停在 10 时，a 再到一条未来事件 t=15（属 [10,20)，正常 on_time）" \
  curl -s -X POST "$BASE/events" -H 'Content-Type: application/json' \
  -d '{"partition":"a","eventId":"e15","eventTime":15}'

req "5. 分区 a 水位 → 12：[0,10) 容忍期结束，最终关闭 final（最终计数 4）" \
  curl -s -X POST "$BASE/watermarks" -H 'Content-Type: application/json' \
  -d '{"partition":"a","watermark":12}'

req "6. 超过容忍期的事件 t=1：本分区水位 12 已满足 end+lat<=12，进入侧输出 late_dropped" \
  curl -s -X POST "$BASE/events" -H 'Content-Type: application/json' \
  -d '{"partition":"a","eventId":"stale1","eventTime":1}'

req "7. 重复事件 e7（即使原窗口已关闭仍可识别）：duplicate，不计数、不进侧输出" \
  curl -s -X POST "$BASE/events" -H 'Content-Type: application/json' \
  -d '{"partition":"a","eventId":"e7","eventTime":999}'

req "8. 慢分区 b 接入：事件 t=103,115（尚未上报水位，不影响全局水位，也不被高水位误伤）" \
  bash -c "curl -s -X POST '$BASE/events' -H 'Content-Type: application/json' -d '{\"partition\":\"b\",\"eventId\":\"b103\",\"eventTime\":103}'; \
           curl -s -X POST '$BASE/events' -H 'Content-Type: application/json' -d '{\"partition\":\"b\",\"eventId\":\"b115\",\"eventTime\":115}'"

req "9. b 水位 → 100：它的窗口按【自己的水位】推进，与 a 无关；[100,110) 末端 110 未到，无输出" \
  curl -s -X POST "$BASE/watermarks" -H 'Content-Type: application/json' \
  -d '{"partition":"b","watermark":100}'

req "10. 快分区 a 继续前进到 22：全局水位 = min(a22,b100)=22（由更小的 a 决定）；a 的 [10,20) 按自身水位 final" \
  curl -s -X POST "$BASE/watermarks" -H 'Content-Type: application/json' \
  -d '{"partition":"a","watermark":22}'

req "11. b 水位 → 110：b 的 [100,110) 此刻 fire（计数 1，容忍期内不 final）" \
  curl -s -X POST "$BASE/watermarks" -H 'Content-Type: application/json' \
  -d '{"partition":"b","watermark":110}'

req "12. b 被标记为空闲（不再参与全局 min）。空闲期间其迟到数据到达外部系统并被缓存；\
 全局水位解除阻挡：a 推进到 130，全局水位随之涨到 130" \
  bash -c "curl -s -X POST '$BASE/partitions/idle' -H 'Content-Type: application/json' -d '{\"partition\":\"b\",\"idle\":true}'; \
           curl -s -X POST '$BASE/watermarks' -H 'Content-Type: application/json' -d '{\"partition\":\"a\",\"watermark\":130}'"

req "13. 分区 b 恢复：先补一条空闲期缓存的事件 t=108（本分区水位 110、窗口 [100,110) 已 fire 但仍在容忍期）\
 → late_accepted 修订，count 变 2、revision 变 2（空闲恢复后容忍期内数据不丢）" \
  curl -s -X POST "$BASE/events" -H 'Content-Type: application/json' \
  -d '{"partition":"b","eventId":"b108","eventTime":108}'

req "14. b 显式水位 → 112：[100,110) 容忍期结束，以最终计数 2 final；\
 全局水位因单调性仍保持 130（恢复的慢分区不会把全局水位拉回去）" \
  curl -s -X POST "$BASE/watermarks" -H 'Content-Type: application/json' \
  -d '{"partition":"b","watermark":112}'

req "15. 恢复后又一条更陈旧的缓存事件 t=105 到达：窗口 [100,110) 已最终关闭（本分区水位 112）→ late_dropped 侧输出" \
  curl -s -X POST "$BASE/events" -H 'Content-Type: application/json' \
  -d '{"partition":"b","eventId":"b-stale105","eventTime":105}'

req "16. b 追上到 130：[110,120) 的 fire/final 时刻（120/122）被一次跳过，直接输出一条 final count=1" \
  curl -s -X POST "$BASE/watermarks" -H 'Content-Type: application/json' \
  -d '{"partition":"b","watermark":130}'

req "17. 查询侧输出（a 的 t=1 与 b 恢复后超期的 t=105，共 2 条）" curl -s "$BASE/side-output"
req "18. 查询重复事件" curl -s "$BASE/duplicates"
req "19. 查询全部窗口输出（排放日志，含触发/修订/最终关闭的确定顺序）" curl -s "$BASE/emissions"
req "20. 全量状态快照" curl -s "$BASE/snapshot"

echo
echo "================ 演示结束（服务即将停止）================"

# 等待服务真正退出，释放端口，避免与后续进程冲突。
kill "$SERVER_PID" 2>/dev/null || true
trap - EXIT
for _ in $(seq 1 30); do
  kill -0 "$SERVER_PID" 2>/dev/null || exit 0
  sleep 0.1
done
exit 0
