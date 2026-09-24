#!/usr/bin/env bash
# 端到端演示（sim 可控时钟）：
#  1) 自主运行 -> 2) 遥控接管 -> 3) 遥控租约到期，旧自主不得复活
#  4) 急停锁存（最高优先、零速、锁存） -> 5) 解除急停，旧命令全部不可用
#  6) 同刻竞争 RC 胜 -> 7) 显式释放租约 -> 8) 新鲜自主恢复
#  9) 查询/评估绝不刷新租约。
# 全部请求携带真实 HMAC-SHA256 签名。
set -uo pipefail
cd "$(dirname "$0")/.."

BASE_URL="${BASE_URL:-http://127.0.0.1:8080}"
export BASE_URL

say()  { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
show() { echo "    $1"; }

C="scripts/call.sh"

say "0. 健康检查"
curl -sS "$BASE_URL/health" | jq .

say "1. t=1000 自主速度命令（lease 9000ms / ttl 20000ms）"
$C autonomous POST /v1/sources/autonomous/commands 1000 \
  '{"seq":1,"lease_id":"au-1","vx":1.5,"wz":0.2,"issued_at":990,"lease_ms":9010,"ttl_ms":20000}' \
  | jq '{accepted,effective,decision}'

say "2. t=2000 遥控接管（issued 1990, lease 到 5000）—— 旧自主被 stale_after_override 抑制"
$C remote POST /v1/sources/remote/commands 2000 \
  '{"seq":1,"lease_id":"rc-1","vx":0.8,"wz":-0.1,"issued_at":1990,"lease_ms":3010,"ttl_ms":20000}' \
  | jq '{accepted,effective,decision:{selected:.decision.selected,suppressed:.decision.suppressed}}'

say "3. t=5001 遥控租约到期；GET 查询证明输出零速且旧自主不复活（查询不刷新租约）"
curl -sS "$BASE_URL/v1/decision?at=5001" | jq .

say "4. t=6000 急停锁存（最高优先级，锁存保持）"
$C estop POST /v1/estop 6000 '{"seq":1,"event":"latch","issued_at":6000,"reason":"red button"}' \
  | jq '{accepted,effective,decision:{estop_latched:.decision.estop_latched,output:.decision.output,stop_reason:.decision.stop_reason}}'

say "4b. 锁存期间到达的任何命令都被抑制（接受存储，但输出仍为零）"
$C remote POST /v1/sources/remote/commands 6100 \
  '{"seq":2,"lease_id":"rc-2","vx":3.0,"wz":0.0,"issued_at":6100,"lease_ms":5000,"ttl_ms":20000}' \
  | jq '{accepted,effective,decision:{suppressed:.decision.suppressed,output:.decision.output}}'

say "5. t=8000 解除急停 —— 锁存前/中的命令 issued_at 均不晚于解除时刻，全部不可用"
$C estop POST /v1/estop 8000 '{"seq":2,"event":"release","issued_at":8000}' \
  | jq '{accepted,effective}'
curl -sS "$BASE_URL/v1/decision?at=8001" | jq .

say "6. t=8100 同刻竞争：自主与遥控 issued_at 同为 8100 —— 遥控胜出"
$C autonomous POST /v1/sources/autonomous/commands 8100 \
  '{"seq":2,"lease_id":"au-2","vx":1.0,"wz":0.0,"issued_at":8100,"lease_ms":5000,"ttl_ms":20000}' >/dev/null
$C remote POST /v1/sources/remote/commands 8100 \
  '{"seq":3,"lease_id":"rc-3","vx":2.0,"wz":0.0,"issued_at":8100,"lease_ms":5000,"ttl_ms":20000}' \
  | jq '{decision:{selected:.decision.selected,suppressed:.decision.suppressed}}'

say "7. t=9000 遥控显式释放租约；t=9000 查询：旧自主 issued<=9000 不复活"
$C remote POST /v1/sources/remote/leases/release 9000 \
  '{"seq":4,"lease_id":"rc-3","issued_at":9000}' >/dev/null
curl -sS "$BASE_URL/v1/decision?at=9000" | jq .

say "8. t=9001 新鲜自主命令（issued 严格晚于释放时刻）恢复运动"
$C autonomous POST /v1/sources/autonomous/commands 9001 \
  '{"seq":3,"lease_id":"au-3","vx":0.5,"wz":0.1,"issued_at":9001,"lease_ms":5000,"ttl_ms":20000}' \
  | jq '{decision:{selected:.decision.selected,output:.decision.output}}'

say "9. 拒绝旧序号重放（seq=1）与到达即过期的消息"
$C remote POST /v1/sources/remote/commands 9100 \
  '{"seq":1,"lease_id":"old","vx":1,"wz":0,"issued_at":9100,"lease_ms":1000,"ttl_ms":10000}' \
  && echo "UNEXPECTED 2xx" || echo "-> 被拒（stale_seq），符合预期"
$C remote POST /v1/sources/remote/commands 9200 \
  '{"seq":5,"lease_id":"late","vx":1,"wz":0,"issued_at":8000,"lease_ms":100,"ttl_ms":100}' \
  && echo "UNEXPECTED 2xx" || echo "-> 被拒（ttl_expired/lease_expired），符合预期"

say "10. 决策历史（只记录写入；GET 从不留痕）"
curl -sS "$BASE_URL/v1/decisions?limit=20" \
  | jq '.decisions[] | {id,kind,accepted,effective,selected:.decision.selected,stop_reason:.decision.stop_reason}'
