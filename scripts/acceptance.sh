#!/usr/bin/env bash
# 端到端验收脚本（curl + python JSON 解析）。
# 验收点：
#   1) 双向窄道：先到者预约成功，相向者 409 且冲突证据包含 edge（对向交换），
#      而不只是 vertex；
#   2) 终点永久占用：再拿 R3 去同一个终点 -> permanent_goal；
#   3) 撤销预订：凭 HMAC cancel_token 撤销后，终点腾出，原来失败的机器人可重新规划；
#   4) 乐观版本：错误版本 -> 412 MAP_STALE/RESV_STALE；
#   5) 批处理：会让湾地图上两机器人原子批量成功；窄道里对向批量原子失败（一个也不落库）。
set -u

BASE="${BASE:-http://127.0.0.1:8000}"
need() { command -v "$1" >/dev/null 2>&1 || { echo "缺少命令: $1" >&2; exit 1; }; }
need curl
PY=.venv/bin/python; [ -x "$PY" ] || PY=python3
# 用法: field '<python expr on d>' <<<"$json"
field() { "$PY" -c 'import json,sys; d=json.load(sys.stdin); print('"$1"')'; }

echo "== 0. 健康检查 & 重置默认场景 =="
curl -sS "$BASE/health" | field 'd["status"]'
curl -sS -X POST "$BASE/admin/reset" >/dev/null

echo
echo "== 1. 配置 4x1 双向窄道，放置 R1=(0,0) R2=(3,0) R3=(1,0) =="
echo "（默认 R1..R4 占满 x=0..3；先临时扩成 5x1 取得空闲格，完成循环移位后再缩回 4x1）"
curl -sS -X PUT "$BASE/map" -H 'Content-Type: application/json' \
  -d '{"width":5,"height":1,"obstacles":[]}' >/dev/null
# 期望：R1=0, R3=1, R4=2, R2=3（(4,0) 仅暂存）
curl -sS -X PUT "$BASE/robots/R4" -H 'Content-Type: application/json' -d '{"start":{"x":4,"y":0}}' >/dev/null
curl -sS -X PUT "$BASE/robots/R2" -H 'Content-Type: application/json' -d '{"start":{"x":3,"y":0}}' >/dev/null
curl -sS -X PUT "$BASE/robots/R3" -H 'Content-Type: application/json' -d '{"start":{"x":1,"y":0}}' >/dev/null
curl -sS -X PUT "$BASE/robots/R4" -H 'Content-Type: application/json' -d '{"start":{"x":2,"y":0}}' >/dev/null
curl -sS -X PUT "$BASE/map" -H 'Content-Type: application/json' \
  -d '{"width":4,"height":1,"obstacles":[]}' | field '"map_version="+str(d["map_version"])'

echo
echo "== 2. R2 先预约：(3,0) -> (0,0)，成功 =="
R2=$(curl -sS -X POST "$BASE/reservations/plan" -H 'Content-Type: application/json' \
  -d '{"robot_id":"R2","goal":{"x":0,"y":0}}')
echo "$R2" | field '"arrival_time="+str(d["arrival_time"])+"  path="+str(d["path"])'
R2_ID=$(echo "$R2" | field 'd["resv_id"]')
R2_TOKEN=$(echo "$R2" | field 'd["cancel_token"]')

echo
echo "== 3. R1 相向预约 (0,0)->(3,0)：必须 409 且证据含 edge（不是只查顶点） =="
R1=$(curl -sS -o /tmp/r1.json -w '%{http_code}' -X POST "$BASE/reservations/plan" \
  -H 'Content-Type: application/json' -d '{"robot_id":"R1","goal":{"x":3,"y":0}}')
echo "HTTP $R1"
cat /tmp/r1.json | field '"code="+d["error"]["code"]'
cat /tmp/r1.json | field '"edge证据条数="+str(sum(1 for b in d["error"]["details"]["evidence"]["blockers"] if b["type"]=="edge"))'
[ "$R1" = "409" ] || { echo "期望 409" >&2; exit 1; }
EDGE_N=$(cat /tmp/r1.json | field 'sum(1 for b in d["error"]["details"]["evidence"]["blockers"] if b["type"]=="edge")')
[ "$EDGE_N" -ge 1 ] || { echo "证据缺少 edge 冲突" >&2; exit 1; }

echo
echo "== 4. R3 去 R2 的永久终点 (0,0)：409 permanent_goal =="
R3=$(curl -sS -o /tmp/r3.json -w '%{http_code}' -X POST "$BASE/reservations/plan" \
  -H 'Content-Type: application/json' -d '{"robot_id":"R3","goal":{"x":0,"y":0}}')
echo "HTTP $R3 / $(cat /tmp/r3.json | field 'd["error"]["details"]["evidence"]["kind"]')"
[ "$R3" = "409" ] || { echo "期望 409" >&2; exit 1; }

echo
echo "== 5. 乐观版本校验：携带错误的预约版本 -> 412 RESV_STALE =="
STALE=$(curl -sS -o /tmp/stale.json -w '%{http_code}' -X POST "$BASE/reservations/plan" \
  -H 'Content-Type: application/json' \
  -d '{"robot_id":"R3","goal":{"x":2,"y":0},"expected_reservation_version":99}')
echo "HTTP $STALE / $(cat /tmp/stale.json | field 'd["error"]["code"]')"
[ "$STALE" = "412" ] || { echo "期望 412" >&2; exit 1; }

echo
echo "== 6. 篡改撤销令牌 -> 403 INVALID_TOKEN =="
BAD_TOKEN="${R2_TOKEN%??}00"
BAD=$(curl -sS -o /tmp/bad.json -w '%{http_code}' -X POST "$BASE/reservations/$R2_ID/cancel" \
  -H 'Content-Type: application/json' -d "{\"cancel_token\":\"$BAD_TOKEN\"}")
echo "HTTP $BAD / $(cat /tmp/bad.json | field 'd["error"]["code"]')"
[ "$BAD" = "403" ] || { echo "期望 403" >&2; exit 1; }

echo
echo "== 7. 用正确令牌撤销 R2，终点腾出 =="
curl -sS -X POST "$BASE/reservations/$R2_ID/cancel" -H 'Content-Type: application/json' \
  -d "{\"cancel_token\":\"$R2_TOKEN\"}" | field '"canceled="+str(d["canceled"])+" 新版本="+str(d["reservation_version"])'

echo
echo "== 8. R1 现在可以横穿窄道 =="
OK=$(curl -sS -o /tmp/ok.json -w '%{http_code}' -X POST "$BASE/reservations/plan" \
  -H 'Content-Type: application/json' -d '{"robot_id":"R1","goal":{"x":3,"y":0}}')
echo "HTTP $OK / $(cat /tmp/ok.json | field 'str(d["path"])')"
[ "$OK" = "200" ] || { echo "期望 200" >&2; exit 1; }

echo
echo "== 9. 批量原子性：恢复窄道，对向批量规划必须整体失败、一个也不落库 =="
curl -sS -X POST "$BASE/admin/reset" >/dev/null
curl -sS -X PUT "$BASE/map" -H 'Content-Type: application/json' \
  -d '{"width":5,"height":1,"obstacles":[]}' >/dev/null
curl -sS -X PUT "$BASE/robots/R4" -H 'Content-Type: application/json' -d '{"start":{"x":4,"y":0}}' >/dev/null
curl -sS -X PUT "$BASE/robots/R2" -H 'Content-Type: application/json' -d '{"start":{"x":3,"y":0}}' >/dev/null
curl -sS -X PUT "$BASE/robots/R4" -H 'Content-Type: application/json' -d '{"start":{"x":2,"y":0}}' >/dev/null
curl -sS -X PUT "$BASE/map" -H 'Content-Type: application/json' \
  -d '{"width":4,"height":1,"obstacles":[]}' >/dev/null
BF=$(curl -sS -o /tmp/bf.json -w '%{http_code}' -X POST "$BASE/reservations/plan-batch" \
  -H 'Content-Type: application/json' \
  -d '{"goals":{"R1":{"goal":{"x":3,"y":0}},"R2":{"goal":{"x":0,"y":0}}}}')
echo "HTTP $BF / 失败机器人=$(cat /tmp/bf.json | field 'd["error"]["details"]["failed_robot"]')"
LEFT=$(curl -sS "$BASE/state" | field 'len(d["reservations"])')
echo "失败后活跃预约数=$LEFT（必须为 0，证明原子）"
[ "$BF" = "409" ] && [ "$LEFT" = "0" ] || { echo "批量原子性失败" >&2; exit 1; }

echo
echo "== 10. 会让湾默认地图：R1 与 R8 相向批量规划，靠港湾错车，原子成功 =="
curl -sS -X POST "$BASE/admin/reset" >/dev/null
BP=$(curl -sS -o /tmp/bp.json -w '%{http_code}' -X POST "$BASE/reservations/plan-batch" \
  -H 'Content-Type: application/json' \
  -d '{"goals":{"R1":{"goal":{"x":7,"y":0}},"R8":{"goal":{"x":0,"y":4}}}}')
echo "HTTP $BP / 顺序=$(cat /tmp/bp.json | field 'str(d["priority_order"])')"
[ "$BP" = "200" ] || { echo "期望 200" >&2; cat /tmp/bp.json; exit 1; }

echo
echo "ALL ACCEPTANCE CHECKS PASSED ✔"
