#!/usr/bin/env bash
# 端到端请求示例。
#
# 前置：以「注入时钟」模式启动服务（便于演示超时；生产用 system 时钟即可）：
#   cargo run --release -- --addr 127.0.0.1:8080 --clock injected --data ./data/events.log
#
# 用法：
#   bash examples/requests.sh            # 默认 http://127.0.0.1:8080
#   BASE=http://127.0.0.1:9000 bash examples/requests.sh
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8080}"
j() { python3 -c 'import sys,json;print(json.load(sys.stdin)["reservation"]["reservation_id"])'; }

echo ">> 健康检查"
curl -sS "$BASE/healthz"; echo

echo ">> 创建租户 acme：1000 字节 / 10 个对象"
curl -sS -X POST "$BASE/tenants" -H 'content-type: application/json' \
  -d '{"tenant_id":"acme","quota_bytes":1000,"quota_objects":10}'; echo

echo ">> 预留前预留 300 字节 / 3 对象，TTL 60s（也可自定义 reservation_id / idempotency_key）"
R1=$(curl -sS -X POST "$BASE/tenants/acme/reservations" -H 'content-type: application/json' \
  -d '{"size_bytes":300,"objects":3,"ttl_ms":60000}' | tee /dev/stderr | j)
echo "R1=$R1" >&2

echo ">> 再预留 700 字节 / 7 对象（额度占满）"
R2=$(curl -sS -X POST "$BASE/tenants/acme/reservations" -H 'content-type: application/json' \
  -d '{"size_bytes":700,"objects":7,"ttl_ms":60000}' | tee /dev/stderr | j)
echo "R2=$R2" >&2

echo ">> 超额 1 字节 -> 429 quota_exceeded"
curl -sS -o /dev/stdout -w ' [%{http_code}]\n' -X POST "$BASE/tenants/acme/reservations" \
  -H 'content-type: application/json' -d '{"size_bytes":1,"objects":1,"ttl_ms":60000}'

echo ">> 提交 R1：预留转实占（重复提交幂等）"
curl -sS -X POST "$BASE/tenants/acme/reservations/$R1/commit"; echo
curl -sS -o /dev/null -X POST "$BASE/tenants/acme/reservations/$R1/commit"; echo "(重复提交返回同一记录)"

echo ">> 取消 R2：释放预留（重复取消 released=false，不会多释放）"
curl -sS -X POST "$BASE/tenants/acme/reservations/$R2/cancel"; echo
curl -sS -X POST "$BASE/tenants/acme/reservations/$R2/cancel"; echo

echo ">> 取消已提交的 R1 -> 409"
curl -sS -o /dev/stdout -w ' [%{http_code}]\n' -X POST "$BASE/tenants/acme/reservations/$R1/cancel"

echo ">> 租户余量视图"
curl -sS "$BASE/tenants/acme"; echo

echo ">> 推进注入时钟 60001ms，使所有 TTL=60s 的预留到期（system 时钟模式无此接口）"
curl -sS -X POST "$BASE/admin/clock/advance" -H 'content-type: application/json' \
  -d '{"delta_ms":60001}'; echo

echo ">> 也可随时主动触发一次到期扫描（两种时钟模式都支持）"
curl -sS -X POST "$BASE/admin/expire-due"; echo

echo ">> 单条预留查询"
curl -sS "$BASE/tenants/acme/reservations/$R1"; echo
