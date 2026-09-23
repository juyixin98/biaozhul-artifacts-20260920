#!/usr/bin/env bash
# 端到端验收演示：
#   正常摄入 -> 注入 AFTER_STATE_PERSISTED/HALT 故障（进程被杀）
#   -> 只读校验器确认崩溃现场 -> 重启自动恢复 -> 核对无遗漏、无重复
# 依赖：JDK 17+ 与 curl。数据目录：./data-demo（脚本会清空重建）。
set -euo pipefail
cd "$(dirname "$0")/.."

find_bin() {
  local name="$1"; local rel="$2"
  if [ -n "${JAVA_HOME:-}" ] && [ -x "$JAVA_HOME/bin/$rel" ]; then echo "$JAVA_HOME/bin/$rel"; return; fi
  if command -v "$name" >/dev/null 2>&1; then command -v "$name"; return; fi
  ls -d .tools/jdk-*/bin/$rel 2>/dev/null | sort -V | tail -1 || true
}
JAVA="$(find_bin java java)"
[ -n "$JAVA" ] || { echo "ERROR: java not found" >&2; exit 1; }

./scripts/build.sh >/dev/null
DATA="$(pwd)/data-demo"
PORT=18080
rm -rf "$DATA"

echo "=== 1) 启动服务（开启故障注入端点） ==="
$JAVA -cp build/classes txsnapshot.Main --port $PORT --data "$DATA" --debug &
SRV=$!
cleanup() { kill "$SRV" 2>/dev/null || true; }
trap cleanup EXIT

wait_http() {
  for _ in $(seq 1 50); do
    curl -sf "http://127.0.0.1:$PORT/health" >/dev/null && return 0
    sleep 0.1
  done
  echo "server did not start" >&2; exit 1
}
wait_http
curl -s "http://127.0.0.1:$PORT/health"; echo

echo "=== 2) 正常摄入 0..2（value=10+offset） ==="
for i in 0 1 2; do
  curl -s -X POST "http://127.0.0.1:$PORT/ingest" \
    -H 'Content-Type: application/json' -d "{\"value\":$((10+i))}"; echo
done
curl -s "http://127.0.0.1:$PORT/state"; echo

echo "=== 3) 武装故障：处理 offset=3 时，状态落盘后、提交前，HALT 杀进程 ==="
curl -s -X POST "http://127.0.0.1:$PORT/debug/fault" \
  -H 'Content-Type: application/json' \
  -d '{"point":"AFTER_STATE_PERSISTED","mode":"HALT","offset":3}'; echo

echo "=== 4) 触发故障（服务进程应已退出） ==="
curl -s -X POST "http://127.0.0.1:$PORT/ingest" \
  -H 'Content-Type: application/json' -d '{"value":13}' >/dev/null 2>&1 || true
for _ in $(seq 1 30); do kill -0 "$SRV" 2>/dev/null || break; sleep 0.1; done
if kill -0 "$SRV" 2>/dev/null; then echo "ERROR: server survived the fault" >&2; exit 1; fi
echo "server process is dead (as designed)"
trap - EXIT

echo "=== 5) 崩溃现场：commit.log / 快照 / 暂存段 ==="
echo "--- sink/commit.log:"; cat "$DATA/sink/commit.log" | sed 's/^/    /'
echo "--- latest snapshot:"; cat "$DATA/checkpoints/current.txt" 2>/dev/null \
  || readlink "$DATA/checkpoints/current"
SNAP=$(basename "$(cat "$DATA/checkpoints/current.txt" 2>/dev/null || readlink "$DATA/checkpoints/current")")
cat "$DATA/checkpoints/$SNAP" | sed 's/^/    /'
echo "--- segments:"; ls -1 "$DATA/sink/segments" | sed 's/^/    /'

echo "=== 6) 只读校验器（恢复前） ==="
echo "    本故障点（落盘后、提交前）是协议内合法的未决窗口：无遗漏无重复，"
echo "    pendingCommit 指向已 prepare 的段，重启会补提交，因此这里应 PASS。"
set +e
$JAVA -cp build/classes txsnapshot.Main --verify --data "$DATA"
VRF=$?
set -e
echo "verifier exit code (pre-recovery) = $VRF （0=一致；DURING_COMMIT 半行崩溃时此处为 1，由自动化测试覆盖）"

echo "=== 7) 重启服务：自动补提交悬空事务并从输入日志重放 ==="
$JAVA -cp build/classes txsnapshot.Main --port $PORT --data "$DATA" --debug &
SRV=$!
trap cleanup EXIT
wait_http

echo "=== 8) 继续摄入 offset=4,5 ==="
for i in 4 5; do
  curl -s -X POST "http://127.0.0.1:$PORT/ingest" \
    -H 'Content-Type: application/json' -d "{\"value\":$((10+i))}"; echo
done

echo "=== 9) 核对结果 ==="
curl -s "http://127.0.0.1:$PORT/state" | sed 's/^/state:   /'; echo
curl -s "http://127.0.0.1:$PORT/outputs" | sed 's/^/outputs: /'; echo

echo "=== 10) 只读校验器（恢复后；预期 PASS，退出码 0） ==="
$JAVA -cp build/classes txsnapshot.Main --verify --data "$DATA"
echo "DEMO OK"
