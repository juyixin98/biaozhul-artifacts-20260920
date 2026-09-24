#!/usr/bin/env bash
# demo.sh — 端到端验收演示（纯后端，全部用 curl）。
#
# 依次演示：
#   1. 基本写/读（N=5 W=3 R=3）
#   2. 部分写成功后超时 → 读到的是 uncommitted 版本
#   3. 并发盲写 → 真实冲突（即使 W+R>N）
#   4. 读修复
#   5. 副本恢复（反熵）
#   6. 冲突解决（因果后继写）
#
# 用法：
#   ./demo.sh [BASE_URL]
# 不传地址时脚本会自己 `go run .` 起服务，结束后自动关闭。
set -u

BASE="${1:-}"
OWN_PID=""
cleanup() {
  # `go run` 会派生子进程，按进程组整体退出，避免留下监听端口的子进程。
  [ -n "$OWN_PID" ] && kill -- "-$OWN_PID" 2>/dev/null
}
trap cleanup EXIT

# 允许通过环境变量覆盖 go / 端口；找不到 go 时尝试常见安装位置。
GO="${GO_BIN:-$(command -v go || echo /usr/local/go/bin/go)}"
PORT="${PORT:-19090}"

if [ -z "$BASE" ]; then
  BASE="http://127.0.0.1:${PORT}"
  echo ">> 启动服务: $GO run . -n 5 -w 3 -r 3 -addr :${PORT}"
  setsid "$GO" run . -n 5 -w 3 -r 3 -addr ":${PORT}" >/tmp/quorumlab-demo.log 2>&1 &
  OWN_PID=$!
  for _ in $(seq 1 100); do
    curl -sf "$BASE/config" >/dev/null 2>&1 && break
    sleep 0.1
  done
fi

if command -v jq >/dev/null 2>&1; then
  j() { jq .; }
else
  j() { python3 -m json.tool 2>/dev/null || cat; }
fi
post() { echo; echo "### POST $1"; echo "    $2"; curl -s -X POST "$BASE$1" -H 'Content-Type: application/json' -d "$2" | j; }
put()  { echo; echo "### PUT $1"; echo "    $2"; curl -s -X PUT  "$BASE$1" -H 'Content-Type: application/json' -d "$2" | j; }
get()  { echo; echo "### GET $1"; curl -s "$BASE$1" | j; }

echo "================================================================"
echo "场景 0：清空状态"
echo "================================================================"
post /reset '{}'

echo
echo "================================================================"
echo "场景 1：基本写/读（仲裁成功，无冲突）"
echo "================================================================"
post /write '{"key":"color","value":"blue"}'
get  '/read?key=color'

echo
echo "================================================================"
echo "场景 2：部分写成功后超时（W=3，副本 3,4 down，副本 2 慢 800ms，超时 200ms）"
echo "  预期：协调者只收到副本 0,1 的确认（< W=3），整体超时（HTTP 504）；"
echo "        副本 0,1 已留下 uncommitted 版本。800ms 后副本 2 的意向晚到"
echo "        落地，仍是 uncommitted；R=1 读取时被明确标记 has_uncommitted=true。"
echo "================================================================"
post /fault '{"replica":3,"mode":"down"}'
post /fault '{"replica":4,"mode":"down"}'
post /fault '{"replica":2,"mode":"delay","delay_ms":800}'
post /write '{"key":"split","value":"maybe","timeout_ms":200}'
echo "---- 立即从副本 0 读（R=1）：部分写已落地，但未达到仲裁 ----"
get  '/read?key=split&r=1&targets=0'
echo "---- 等待 1s 让慢副本 2 的意向晚到，再从副本 2 读 ----"
sleep 1
get  '/read?key=split&r=1&targets=2'
post /fault '{"replica":2,"mode":"up"}'
post /fault '{"replica":3,"mode":"up"}'
post /fault '{"replica":4,"mode":"up"}'

echo
echo "================================================================"
echo "场景 3：并发盲写（无因果上下文），即使 W+R=6>N=5 仍产生冲突"
echo "  写 A 到副本 0,1,2；写 B 到副本 0,3,4；副本 0 同时持有两个兄弟版本。"
echo "  预期：读返回 conflict=true 和两个并发版本，系统不擅自选一个、"
echo "        也不把该历史标记为线性一致。"
echo "================================================================"
post /write '{"key":"race","value":"A","targets":[0,1,2]}'
post /write '{"key":"race","value":"B","targets":[0,3,4]}'
get  '/read?key=race&targets=1,3,4&r=3'

echo
echo "================================================================"
echo "场景 4：读修复"
echo "  写只落在副本 1,2；从副本 1 和 3 读并 repair=1，副本 3 被补齐。"
echo "================================================================"
post /write '{"key":"rr","value":"fixed","w":2,"targets":[1,2]}'
get  '/read?key=rr&targets=1,3&r=2&repair=1'
echo
echo "---- 副本 3 当前内容（应已含 rr） ----"
get '/state' | grep -A3 '"id": 3' || get '/state'

echo
echo "================================================================"
echo "场景 5：副本恢复（副本 4 down 期间错过写，recover 后反熵补齐）"
echo "================================================================"
post /fault  '{"replica":4,"mode":"down"}'
post /write  '{"key":"back","value":"online","targets":[0,1,2]}'
post /recover '{"replica":4}'
get  '/read?key=back&targets=4&r=1'

echo
echo "================================================================"
echo "场景 6：冲突解决（以全部兄弟版本为共同因果后继写一次）"
echo "================================================================"
post /resolve '{"key":"race","value":"A-then-B-resolved","merge_all":true}'
get  '/read?key=race&repair=1&full=1'

echo
echo "演示结束。"
