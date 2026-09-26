#!/usr/bin/env bash
# curl-examples.sh —— 幂等存款服务请求样例（可直接执行，需要先启动服务）。
#
# 用法：
#   ./bin/idempotency-server -addr 127.0.0.1:18080 -fake-clock -pending-ttl 30s &
#   ./bin/auditserver    -addr 127.0.0.1:18081 &
#   bash examples/curl-examples.sh
#
# 纯本地演示脚本，不接任何生产系统。
set -u

BASE="${BASE:-http://127.0.0.1:18080}"
AUDIT="${AUDIT:-http://127.0.0.1:18081}"
CURL="curl -sS -i"

line() { printf '\n===== %s =====\n' "$1"; }

line "健康检查"
$CURL "$BASE/healthz"; echo

line "1) 首次请求（期望 200, Idempotency-Status: created）"
$CURL -X POST "$BASE/v1/deposits" \
  -H 'Idempotency-Key: demo-key-0001' \
  -H 'Content-Type: application/json' \
  -d '{"account":"alice","amount":100,"memo":"lunch"}'; echo

line "2) 同键同正文重试（期望 200, Idempotency-Status: replayed，同一 tx_id）"
$CURL -X POST "$BASE/v1/deposits" \
  -H 'Idempotency-Key: demo-key-0001' \
  -H 'Content-Type: application/json' \
  -d '{"account":"alice","amount":100,"memo":"lunch"}'; echo

line "3) 同键不同正文（期望 409 idempotency_conflict）"
$CURL -X POST "$BASE/v1/deposits" \
  -H 'Idempotency-Key: demo-key-0001' \
  -H 'Content-Type: application/json' \
  -d '{"account":"alice","amount":999}'; echo

line "4) 查看幂等键记录"
$CURL "$BASE/v1/idempotency/demo-key-0001"; echo

line "5) 查看本地副作用（账本流水 + 余额，应只有一笔 100）"
$CURL "$BASE/admin/ledger"; echo

line "6) 处理中重复请求（首个请求提交前停留 400ms）"
# 先后台发起慢请求，再立刻发同键同正文：第二个请求会等待并重放
$CURL -X POST "$BASE/v1/deposits" \
  -H 'Idempotency-Key: demo-key-0002' \
  -H 'X-Fault-Delay-Before-Commit-Ms: 400' \
  -H 'Content-Type: application/json' \
  -d '{"account":"bob","amount":50}' >/tmp/demo_first.out 2>&1 &
sleep 0.1
$CURL -X POST "$BASE/v1/deposits" \
  -H 'Idempotency-Key: demo-key-0002' \
  -H 'Content-Type: application/json' \
  -d '{"account":"bob","amount":50}'; echo
wait
printf '(首个请求响应见 /tmp/demo_first.out)\n'

line "7a) 提交前断线（期望 curl 报 Empty reply / 连接被关闭）"
$CURL -X POST "$BASE/v1/deposits" \
  -H 'Idempotency-Key: demo-key-0003' \
  -H 'X-Fault-Before-Commit-Disconnect: 1' \
  -H 'Content-Type: application/json' \
  -d '{"account":"carol","amount":300}'; echo "(curl 退出码 $?)"

line "7b) TTL 未到立即重试（期望 202 in_progress，明确告知稍后重试）"
$CURL -X POST "$BASE/v1/deposits" \
  -H 'Idempotency-Key: demo-key-0003' \
  -H 'Content-Type: application/json' \
  -d '{"account":"carol","amount":300}'; echo

line "7c) 拨快假时钟超过 TTL"
$CURL -X POST "$BASE/admin/clock/advance" -H 'Content-Type: application/json' \
  -d '{"ms":31000}'; echo

line "7d) TTL 后同键重试（期望 200 created，本地账本仍只有一笔）"
$CURL -X POST "$BASE/v1/deposits" \
  -H 'Idempotency-Key: demo-key-0003' \
  -H 'Content-Type: application/json' \
  -d '{"account":"carol","amount":300}'; echo

line "8a) 提交后断线（事务已落盘，响应回程丢失；curl 报错）"
$CURL -X POST "$BASE/v1/deposits" \
  -H 'Idempotency-Key: demo-key-0004' \
  -H 'X-Fault-After-Commit-Disconnect: 1' \
  -H 'Content-Type: application/json' \
  -d '{"account":"dave","amount":900}'; echo "(curl 退出码 $?)"

line "8b) 重试（期望 200 replayed，账本与外部调用都仅一次）"
$CURL -X POST "$BASE/v1/deposits" \
  -H 'Idempotency-Key: demo-key-0004' \
  -H 'Content-Type: application/json' \
  -d '{"account":"dave","amount":900}'; echo

line "9a) 让外部系统『先落事件再返回失败』"
$CURL -X POST "$AUDIT/audit/faults" -H 'Content-Type: application/json' \
  -d '{"record_then_fail_next_n":1}'; echo

line "9b) 首次请求（期望 502，外部其实已记录一条事件）"
$CURL -X POST "$BASE/v1/deposits" \
  -H 'Idempotency-Key: demo-key-0005' \
  -H 'Content-Type: application/json' \
  -d '{"account":"erin","amount":500}'; echo

line "9c) 重试（期望 200 created，本地账本一笔；外部系统收到两条事件）"
$CURL -X POST "$BASE/v1/deposits" \
  -H 'Idempotency-Key: demo-key-0005' \
  -H 'Content-Type: application/json' \
  -d '{"account":"erin","amount":500}'; echo

line "9d) 证据：外部事件数（calls=2）vs 本地账本（effects=1）"
$CURL "$AUDIT/audit/inspect"; echo
$CURL "$BASE/admin/ledger"; echo

line "统计与复位"
$CURL "$BASE/admin/stats"; echo
$CURL -X POST "$BASE/admin/reset"; echo
$CURL -X POST "$AUDIT/audit/reset"; echo
