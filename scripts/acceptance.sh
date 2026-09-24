#!/usr/bin/env bash
# 验收脚本（acceptance）：自启服务、执行断言、退出时清理。
# 通过条件：全部断言通过（脚本退出码 0）。真实 HMAC、真实 SQLite、可控 sim 时钟。
set -uo pipefail
cd "$(dirname "$0")/.."

PORT="${PORT:-$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')}"
BASE_URL="http://127.0.0.1:${PORT}"
export BASE_URL
DB="$(mktemp -d)/arbiter.sqlite"
BIN="./target/release/motion-arbiter"
C="scripts/call.sh"
export QUIET=1 RAW=1

PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); printf '  \033[32m✓\033[0m %s\n' "$1"; }
bad()  { FAIL=$((FAIL+1)); printf '  \033[31m✗ %s\033[0m\n' "$1"; [ -n "${2:-}" ] && echo "    $2"; }
assert() { # assert <desc> <condition-exit-0 means pass>; stdin is jq input
  if eval "$2" >/dev/null 2>&1; then ok "$1"; else bad "$1" "$3"; fi
}
jqget() { curl -sS "$BASE_URL$1"; }

cleanup() {
  [ -n "${SRV_PID:-}" ] && kill "$SRV_PID" 2>/dev/null || true
  rm -rf "$(dirname "$DB")" 2>/dev/null || true
}
trap cleanup EXIT

if [ ! -x "$BIN" ]; then
  echo "building release binary ..."
  cargo build --release --offline
fi

echo "starting server on $BASE_URL with db $DB"
ARBITER_CLOCK=sim ARBITER_BIND="127.0.0.1:$PORT" ARBITER_DB="$DB" \
  ARBITER_SIM_START=1000 ARBITER_ADMIN_TOKEN=acceptance-token \
  "$BIN" >/tmp/arbiter-acceptance.log 2>&1 &
SRV_PID=$!

for _ in $(seq 1 100); do
  curl -sf "$BASE_URL/health" >/dev/null 2>&1 && break
  sleep 0.05
done
curl -sf "$BASE_URL/health" >/dev/null || { echo "server failed to start; see /tmp/arbiter-acceptance.log"; exit 1; }

echo; echo "== A. 鉴权（真实 HMAC-SHA256，恒定时间校验） =="
CODE=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE_URL/v1/sources/remote/commands" \
  -H 'X-Sim-At: 1000' -d '{}')
[ "$CODE" = "401" ] && ok "无签名 -> 401" || bad "无签名应 401，得到 $CODE"

CODE=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE_URL/v1/sources/remote/commands" \
  -H 'X-Sim-At: 1000' -H 'X-Signature: hex=00' -d '{}')
[ "$CODE" = "401" ] && ok "坏签名 -> 401" || bad "坏签名应 401，得到 $CODE"

echo; echo "== B. 优先级 + 租约到期不复活 =="
$C autonomous POST /v1/sources/autonomous/commands 1000 \
  '{"seq":1,"lease_id":"au1","vx":1.5,"wz":0,"issued_at":990,"lease_ms":9010,"ttl_ms":20000}' >/dev/null
[ "$(jqget '/v1/decision?at=1000' | jq -r .selected.source)" = "autonomous" ] \
  && ok "自主命令被选中" || bad "自主命令未选中"

$C remote POST /v1/sources/remote/commands 2000 \
  '{"seq":1,"lease_id":"rc1","vx":0.8,"wz":0,"issued_at":1990,"lease_ms":3010,"ttl_ms":20000}' >/dev/null
[ "$(jqget '/v1/decision?at=2000' | jq -r .selected.source)" = "remote" ] \
  && ok "遥控优先抢占" || bad "遥控未抢占"
[ "$(jqget '/v1/decision?at=2000' | jq -r '.suppressed[]|select(.source=="autonomous").reason')" = "stale_after_override" ] \
  && ok "旧自主标记 stale_after_override" || bad "抑制原因错误"

# 大量 GET 查询不能刷新租约
for at in 3000 4000 4999 5000; do jqget "/v1/decision?at=$at" >/dev/null; done
D="$(jqget '/v1/decision?at=5001')"
[ "$(echo "$D" | jq -r .selected)" = "null" ] && ok "遥控租约到期后零速" || bad "到期后仍有输出"
echo "$D" | jq -e '.suppressed[]|select(.source=="autonomous" and .reason=="stale_after_override")' >/dev/null \
  && ok "旧自主绝不复活" || bad "旧自主被错误恢复"
echo "$D" | jq -e '.suppressed[]|select(.source=="remote" and .reason=="lease_expired")' >/dev/null \
  && ok "遥控原因 lease_expired" || bad "缺少 lease_expired"

echo; echo "== C. 旧序号 / 到达即过期拒绝，且拒绝不推进水位 =="
RESP="$($C remote POST /v1/sources/remote/commands 5002 \
  '{"seq":1,"lease_id":"rc1","vx":1,"wz":0,"issued_at":5002,"lease_ms":1000,"ttl_ms":20000}' 2>/dev/null || true)"
echo "$RESP" | jq -e '.reason=="stale_seq"' >/dev/null && ok "旧 seq 拒绝 stale_seq" || bad "旧 seq 未拒绝: $RESP"
RESP="$($C remote POST /v1/sources/remote/commands 5002 \
  '{"seq":2,"lease_id":"x","vx":1,"wz":0,"issued_at":1000,"lease_ms":10,"ttl_ms":10}' 2>/dev/null || true)"
echo "$RESP" | jq -e '.reason|test("expired")' >/dev/null && ok "到达即过期拒绝" || bad "过期消息未拒绝: $RESP"

echo; echo "== D. 急停锁存优先、保持、解除保鲜门控 =="
$C estop POST /v1/estop 6000 '{"seq":1,"event":"latch","issued_at":6000}' >/dev/null
D="$(jqget '/v1/decision?at=6000')"
[ "$(echo "$D" | jq -r .stop_reason)" = "estop_latched" ] && ok "急停锁存零速" || bad "锁存失败"
[ "$(echo "$D" | jq -r '.suppressed|length')" = "2" ] && ok "锁存抑制全部来源" || bad "抑制数量错误"
# 锁存保持：即使经过 tick
$C estop POST /v1/decisions/evaluate 7000 '{}' >/dev/null
[ "$(jqget '/v1/decision?at=7500' | jq -r .estop_latched)" = "true" ] && ok "锁存跨时刻保持" || bad "锁存未保持"
# 未解除前 release 以外，重复 latch 幂等
$C estop POST /v1/estop 7600 '{"seq":2,"event":"latch","issued_at":7600}' >/dev/null \
  && ok "重复 latch 接受且幂等" || bad "重复 latch 失败"
$C estop POST /v1/estop 8000 '{"seq":3,"event":"release","issued_at":8000}' >/dev/null
D="$(jqget '/v1/decision?at=8001')"
[ "$(echo "$D" | jq -r .estop_latched)" = "false" ] && ok "解除成功" || bad "解除失败"
echo "$D" | jq -e '.suppressed[]|select(.reason=="stale_after_estop_release")' >/dev/null \
  && ok "解除后旧命令需新鲜（stale_after_estop_release）" || bad "保鲜门控缺失"
# 同刻消息不新鲜
$C remote POST /v1/sources/remote/commands 8002 \
  '{"seq":3,"lease_id":"same","vx":1,"wz":0,"issued_at":8000,"lease_ms":5000,"ttl_ms":20000}' >/dev/null
[ "$(jqget '/v1/decision?at=8002' | jq -r .selected)" = "null" ] && ok "同刻命令不被选中（严格晚于）" || bad "同刻命令被错误选中"
$C remote POST /v1/sources/remote/commands 8003 \
  '{"seq":4,"lease_id":"fresh","vx":1.25,"wz":0,"issued_at":8001,"lease_ms":5000,"ttl_ms":20000}' >/dev/null
[ "$(jqget '/v1/decision?at=8003' | jq -r .selected.source)" = "remote" ] && ok "新鲜 RC 恢复" || bad "新鲜 RC 未恢复"

echo; echo "== E. 同刻竞争（两源同一 issued_at / 到达时刻，RC 胜，顺序无关） =="
for order in rc-first au-first; do
  $C estop POST /v1/estop 9000 '{"seq":4,"event":"latch","issued_at":9000}' >/dev/null
  $C estop POST /v1/estop 10000 '{"seq":5,"event":"release","issued_at":10000}' >/dev/null
  if [ "$order" = rc-first ]; then
    $C remote POST /v1/sources/remote/commands 10100 \
      '{"seq":5,"lease_id":"tie","vx":1,"wz":0,"issued_at":10100,"lease_ms":5000,"ttl_ms":20000}' >/dev/null
    $C autonomous POST /v1/sources/autonomous/commands 10100 \
      '{"seq":2,"lease_id":"tie","vx":1,"wz":0,"issued_at":10100,"lease_ms":5000,"ttl_ms":20000}' >/dev/null
  else
    $C autonomous POST /v1/sources/autonomous/commands 10100 \
      '{"seq":2,"lease_id":"tie","vx":1,"wz":0,"issued_at":10100,"lease_ms":5000,"ttl_ms":20000}' >/dev/null
    $C remote POST /v1/sources/remote/commands 10100 \
      '{"seq":5,"lease_id":"tie","vx":1,"wz":0,"issued_at":10100,"lease_ms":5000,"ttl_ms":20000}' >/dev/null
  fi
  [ "$(jqget '/v1/decision?at=10100' | jq -r .selected.source)" = "remote" ] \
    && ok "同刻竞争 RC 胜（$order）" || bad "同刻竞争失败（$order）"
done

echo; echo "== F. 查询与 tick 不刷新租约 =="
$C estop POST /v1/estop 11000 '{"seq":6,"event":"latch","issued_at":11000}' >/dev/null
$C estop POST /v1/estop 12000 '{"seq":7,"event":"release","issued_at":12000}' >/dev/null
$C remote POST /v1/sources/remote/commands 12000 \
  '{"seq":6,"lease_id":"q","vx":1,"wz":0,"issued_at":12000,"lease_ms":1000,"ttl_ms":10000}' >/dev/null
for at in 12100 12500 12999 12999; do jqget "/v1/decision?at=$at" >/dev/null; done
$C estop POST /v1/decisions/evaluate 12999 '{}' >/dev/null
[ "$(jqget '/v1/decision?at=13000' | jq -r .selected)" = "null" ] \
  && ok "查询/tick 后租约仍按时到期" || bad "租约被查询刷新"

echo; echo "== G. 显式释放租约不恢复旧自主，须新鲜命令 =="
$C autonomous POST /v1/sources/autonomous/commands 14000 \
  '{"seq":3,"lease_id":"aold","vx":1,"wz":0,"issued_at":14000,"lease_ms":20000,"ttl_ms":30000}' >/dev/null
$C remote POST /v1/sources/remote/commands 14500 \
  '{"seq":7,"lease_id":"rcr","vx":1,"wz":0,"issued_at":14500,"lease_ms":10000,"ttl_ms":30000}' >/dev/null
$C remote POST /v1/sources/remote/leases/release 15000 \
  '{"seq":8,"lease_id":"rcr","issued_at":15000}' >/dev/null
D="$(jqget '/v1/decision?at=15000')"
[ "$(echo "$D" | jq -r .selected)" = "null" ] && ok "释放后零速" || bad "释放后仍有输出"
echo "$D" | jq -e '.suppressed[]|select(.source=="autonomous" and .reason=="stale_after_override")' >/dev/null \
  && ok "释放不复活旧自主" || bad "释放后旧自主复活"
$C autonomous POST /v1/sources/autonomous/commands 15001 \
  '{"seq":4,"lease_id":"afresh","vx":0.7,"wz":0,"issued_at":15001,"lease_ms":5000,"ttl_ms":20000}' >/dev/null
[ "$(jqget '/v1/decision?at=15001' | jq -r .selected.source)" = "autonomous" ] \
  && ok "新鲜自主接手" || bad "新鲜自主未接手"

echo; echo "== H. 决策记录审计 =="
ND="$(jqget '/v1/decisions?limit=200' | jq '[.decisions[]|select(.kind=="evaluate")]|length')"
[ "$ND" -ge 2 ] && ok "写入/tick 均留决策记录（evaluate=$ND）" || bad "决策记录不足"
jqget '/v1/ingest-events?limit=200' | jq -e '.events[]|select(.reason=="stale_seq" and .accepted==false)' >/dev/null \
  && ok "拒绝消息进入 ingest-events 审计" || bad "缺少拒绝审计"

echo; echo "== I. 重启持久化 =="
kill "$SRV_PID"; wait "$SRV_PID" 2>/dev/null; SRV_PID=""
ARBITER_CLOCK=sim ARBITER_BIND="127.0.0.1:$PORT" ARBITER_DB="$DB" \
  ARBITER_ADMIN_TOKEN=acceptance-token "$BIN" >>/tmp/arbiter-acceptance.log 2>&1 &
SRV_PID=$!
for _ in $(seq 1 100); do curl -sf "$BASE_URL/health" >/dev/null 2>&1 && break; sleep 0.05; done
ST="$(jqget /v1/state)"
[ "$(echo "$ST" | jq -r .autonomous.last_seq)" = "4" ] && ok "重启后自主 seq 水位保持" || bad "seq 水位丢失"
[ "$(echo "$ST" | jq -r .estop.seq)" = "7" ] && ok "重启后急停 seq 水位保持" || bad "急停水位丢失"
# 历史跨重启
[ "$(jqget '/v1/decisions?limit=200' | jq '.decisions|length')" -ge 10 ] && ok "决策历史跨重启保留" || bad "历史丢失"

echo
echo "========================================================"
echo "  PASS=$PASS  FAIL=$FAIL"
echo "========================================================"
[ "$FAIL" -eq 0 ]
