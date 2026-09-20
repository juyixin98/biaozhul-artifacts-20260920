#!/usr/bin/env bash
# CloudGate 控制平面端到端演示。
# 用法：
#   ./scripts/demo.sh                 # 对接 http://localhost:18099（docker compose 默认端口）
#   BASE_URL=http://localhost:8000 ./scripts/demo.sh
#
# 演示流程：建租户 -> 建接入点/地址池 -> 注册设备 -> 连接分配 IP ->
# 心跳 -> 重复连接幂等 -> 迟到/错代次心跳被拒 -> 重连新代次 -> 撤销设备。
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:18099}"
SUPER_KEY="${SUPER_KEY:-dev-super-key-change-me}"

c() { curl -sS -H "Content-Type: application/json" "$@"; }
j() { python3 -c "import sys,json;d=json.load(sys.stdin);print(d$1)"; }

echo "== 0) 健康检查"
c "$BASE_URL/health"; echo

echo "== 1) 创建租户（管理员密钥仅返回一次）"
TENANT=$(c -X POST "$BASE_URL/admin/tenants" -H "X-Admin-Key: $SUPER_KEY" \
  -d "{\"name\":\"demo-tenant-$(date +%s)\"}")
echo "$TENANT"
ADMIN_KEY=$(echo "$TENANT" | j "['admin_key']")
AUTH=(-H "X-Admin-Key: $ADMIN_KEY")

echo "== 2) 创建接入点（容量 3）与地址池 10.0.0.0/29（可用 .1-.6，自动排除网络/广播）"
AP=$(c -X POST "$BASE_URL/access-points" "${AUTH[@]}" -d '{"name":"edge-sh","capacity":3}')
AP_ID=$(echo "$AP" | j "['id']")
c -X POST "$BASE_URL/access-points/$AP_ID/pools" "${AUTH[@]}" \
  -d '{"cidr":"10.0.0.0/29","reserved_first":0,"reserved_last":0}'; echo

echo "== 3) 注册 4 台设备（令牌仅返回一次）"
D1=$(c -X POST "$BASE_URL/devices" "${AUTH[@]}" -d '{"name":"edge-device-1"}')
D2=$(c -X POST "$BASE_URL/devices" "${AUTH[@]}" -d '{"name":"edge-device-2"}')
D3=$(c -X POST "$BASE_URL/devices" "${AUTH[@]}" -d '{"name":"edge-device-3"}')
D4=$(c -X POST "$BASE_URL/devices" "${AUTH[@]}" -d '{"name":"edge-device-4"}')
T1=$(echo "$D1" | j "['token']"); T2=$(echo "$D2" | j "['token']")
T3=$(echo "$D3" | j "['token']"); T4=$(echo "$D4" | j "['token']")
DEV1=(-H "X-Device-Token: $T1"); DEV2=(-H "X-Device-Token: $T2")

echo "== 4) 设备 1/2/3 连接，获得唯一 IP（.1/.2/.3）"
L1=$(c -X POST "$BASE_URL/device/sessions/connect" "${DEV1[@]}" \
  -d "{\"access_point_id\":\"$AP_ID\"}")
echo "$L1"
c -X POST "$BASE_URL/device/sessions/connect" "${DEV2[@]}" \
  -d "{\"access_point_id\":\"$AP_ID\"}" >/dev/null && echo "device-2 connected"
c -X POST "$BASE_URL/device/sessions/connect" -H "X-Device-Token: $T3" \
  -d "{\"access_point_id\":\"$AP_ID\"}" >/dev/null && echo "device-3 connected"
LID1=$(echo "$L1" | j "['lease_id']"); GEN1=$(echo "$L1" | j "['generation']")

echo "== 5) 重复连接幂等（lease_id 不变，reused=true）"
c -X POST "$BASE_URL/device/sessions/connect" "${DEV1[@]}" \
  -d "{\"access_point_id\":\"$AP_ID\"}"; echo

echo "== 6) 容量已满：设备 4 被拒（ap_full, 3/3）"
c -X POST "$BASE_URL/device/sessions/connect" -H "X-Device-Token: $T4" \
  -d "{\"access_point_id\":\"$AP_ID\"}"; echo

echo "== 7) 心跳保活"
c -X POST "$BASE_URL/device/leases/$LID1/heartbeat" "${DEV1[@]}" \
  -d "{\"generation\":$GEN1}" >/dev/null && echo "heartbeat ok"

echo "== 8) 错误代次心跳被拒（stale_generation，HTTP 409）"
c -X POST "$BASE_URL/device/leases/$LID1/heartbeat" "${DEV1[@]}" \
  -d '{"generation":999}'; echo

echo "== 9) 关闭设备 1，地址释放；设备 4 立即拿到 10.0.0.1"
c -X POST "$BASE_URL/device/leases/$LID1/close" "${DEV1[@]}" \
  -d "{\"generation\":$GEN1}" >/dev/null && echo "closed"
L4=$(c -X POST "$BASE_URL/device/sessions/connect" -H "X-Device-Token: $T4" \
  -d "{\"access_point_id\":\"$AP_ID\"}")
echo "$L4"
LID4=$(echo "$L4" | j "['lease_id']")

echo "== 10) 撤销设备 3 腾出容量；设备 1 重连得到新租约与新代次（first-fit 拿到 .3）"
D3_ID=$(echo "$D3" | j "['id']")
c -X POST "$BASE_URL/devices/$D3_ID/revoke" "${AUTH[@]}" >/dev/null && echo "device-3 revoked"
L1B=$(c -X POST "$BASE_URL/device/sessions/connect" "${DEV1[@]}" \
  -d "{\"access_point_id\":\"$AP_ID\"}")
echo "$L1B"
LID1B=$(echo "$L1B" | j "['lease_id']"); GEN1B=$(echo "$L1B" | j "['generation']")
echo "-- 旧代次关闭新租约被拒（stale_generation）"
c -X POST "$BASE_URL/device/leases/$LID1B/close" "${DEV1[@]}" -d '{"generation":1}'; echo
echo "-- 旧租约的迟到心跳被拒（lease_terminated，不复活）"
c -X POST "$BASE_URL/device/leases/$LID1/heartbeat" "${DEV1[@]}" \
  -d "{\"generation\":$GEN1}"; echo

echo "== 11) 撤销设备 4：会话立即终止（revoked），旧令牌拒绝重连，地址释放"
c -X POST "$BASE_URL/devices/$(echo "$D4" | j "['id']")/revoke" "${AUTH[@]}" >/dev/null \
  && echo "revoked"
c -X POST "$BASE_URL/device/sessions/connect" -H "X-Device-Token: $T4" \
  -d "{\"access_point_id\":\"$AP_ID\"}"; echo
c "$BASE_URL/leases/$LID4" "${AUTH[@]}"; echo

echo "== 12) 管理员查看当前活动租约与设备 1 的生命周期事件"
c "$BASE_URL/leases?status=active" "${AUTH[@]}" | python3 -m json.tool
c "$BASE_URL/leases/$LID1B/events" "${AUTH[@]}" | python3 -m json.tool

echo "演示完成。API 文档：$BASE_URL/docs"
