#!/usr/bin/env bash
# 端到端验收脚本：真实启动 broker/PG/consumer，验证
#   1) RETAINED 保留消息：新订阅收到快照，但不刷在线状态、不产生event
#   2) 正常 QoS1 处理 + 在线状态刷新
#   3) 设备重启(boot_gen递增)不混淆序号；旧代次迟到包不回退状态
#   4) 非法 payload 进隔离区且不阻塞其他设备
#   5) 业务提交失败（回滚不ACK）→ broker重投递 → 清除故障后恰好处理一次
#   6) 提交成功但ACK丢失 → 重投递命中业务去重（一条业务行 + 一条dup审计行）
#   7) consumer 进程 kill -9 崩溃重启后，未ACK消息由持久会话重投递
#
# 用法: scripts/acceptance.sh
# 前置: docker、go 1.22+；脚本自管 mqttredel-mqtt / mqttredel-pg 容器。
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

MQTT_PORT="${MQTT_PORT:-11883}"
PG_PORT="${PG_PORT:-55433}"
HTTP_PORT="${HTTP_PORT:-8079}"
MQTT="127.0.0.1:${MQTT_PORT}"
DSN="postgres://mqttredel:mqttredel@127.0.0.1:${PG_PORT}/mqttredel?sslmode=disable"
CID="acc-consumer-$$"
PASS=0; FAIL=0

red()  { printf '\033[31m%s\033[0m\n' "$*"; }
grn()  { printf '\033[32m%s\033[0m\n' "$*"; }
info() { printf '\n\033[36m── %s\033[0m\n' "$*"; }

check() { if [ "$2" = "$3" ]; then grn "  ✓ $1 = $2"; PASS=$((PASS+1)); else red "  ✗ $1 = $2, 期望 $3"; FAIL=$((FAIL+1)); fi; }
ok()    { if [ "$1" = "true" ]; then grn "  ✓ $2"; PASS=$((PASS+1)); else red "  ✗ $2"; FAIL=$((FAIL+1)); fi; }

psql_exec() { docker exec mqttredel-pg psql -U mqttredel -d mqttredel -tAc "$1"; }
metric()    { curl -s "http://127.0.0.1:${HTTP_PORT}/metrics" | python3 -c "import sys,json;print(json.load(sys.stdin)['$1'])"; }
wait_metric() { local key="$1" want="$2" timeout="${3:-30}" n=0
  while [ "$n" -lt "$timeout" ]; do [ "$(metric "$key" 2>/dev/null)" = "$want" ] && return 0; sleep 0.5; n=$((n+1)); done; return 1; }
wait_sql() { local sql="$1" want="$2" timeout="${3:-30}" n=0
  while [ "$n" -lt "$timeout" ]; do [ "$(psql_exec "$sql")" = "$want" ] && return 0; sleep 0.5; n=$((n+1)); done; return 1; }
state_field() { curl -s "http://127.0.0.1:${HTTP_PORT}/state" | python3 -c "
import sys,json
for x in json.load(sys.stdin)['devices']:
  if x['device_id']=='$1': print(x['$2']); break"; }

CONSUMER_PID=""
start_consumer() { # start_consumer [client_id]
  local cid="${1:-$CID}"
  nohup ./bin/consumer -mqtt "$MQTT" -pg "$DSN" -http "127.0.0.1:${HTTP_PORT}" -client-id "$cid" > /tmp/acc-consumer.log 2>&1 &
  CONSUMER_PID=$!
  for i in $(seq 1 40); do curl -s "http://127.0.0.1:${HTTP_PORT}/healthz" 2>/dev/null | grep -q '"connected":true' && return 0; sleep 0.5; done
  red "consumer 未就绪"; tail /tmp/acc-consumer.log; return 1
}
stop_consumer() { [ -n "$CONSUMER_PID" ] && kill -9 "$CONSUMER_PID" 2>/dev/null; CONSUMER_PID=""; sleep 1; }

cleanup() { info "清理"; stop_consumer; docker rm -f mqttredel-mqtt mqttredel-pg >/dev/null 2>&1; docker volume rm mqttredel-mqtt-data >/dev/null 2>&1; }
trap cleanup EXIT

info "启动全新 PostgreSQL 与 Mosquitto（清空持久卷）"
docker rm -f mqttredel-mqtt mqttredel-pg >/dev/null 2>&1
docker volume rm mqttredel-mqtt-data >/dev/null 2>&1
docker run -d --name mqttredel-pg -e POSTGRES_USER=mqttredel -e POSTGRES_PASSWORD=mqttredel \
  -e POSTGRES_DB=mqttredel -p "${PG_PORT}:5432" postgres:16-alpine >/dev/null
docker run -d --name mqttredel-mqtt -p "${MQTT_PORT}:1883" \
  -v "$ROOT/deploy/mosquitto.conf:/mosquitto/config/mosquitto.conf:ro" \
  -v mqttredel-mqtt-data:/mosquitto/data eclipse-mosquitto:2 >/dev/null
for i in $(seq 1 30); do docker exec mqttredel-pg pg_isready -U mqttredel >/dev/null 2>&1 && break; sleep 1; done
for i in $(seq 1 20); do mosquitto_pub -h 127.0.0.1 -p "$MQTT_PORT" -t health -m ok -q 1 2>/dev/null && break; sleep 1; done

info "构建并预置设备"
go build -o bin/ ./cmd/... || { red "编译失败"; exit 1; }
for d in dev-A dev-B dev-C; do ./bin/seed -id "$d" -pg "$DSN" >/dev/null; done

# ── 场景1：RETAINED 保留消息（必须在首次订阅前发布）────────────────
info "场景1：无订阅者时发布 RETAINED；consumer 首次订阅收到快照但不刷状态/不产生event"
./bin/inject -mqtt "$MQTT" retained dev-A >/dev/null
start_consumer "$CID"
grep -q "sessionPresent=false" /tmp/acc-consumer.log && ok true "首次连接 sessionPresent=false（全新会话）" || ok false "首次连接应为全新会话"
wait_metric snapshots 1 || true
check "snapshot 审计行" "$(psql_exec 'SELECT count(*) FROM raw_messages WHERE kind=$$snapshot$$')" 1
check "retained 不产生 event" "$(psql_exec 'SELECT count(*) FROM events')" 0
check "retained 不建立在线状态" "$(psql_exec 'SELECT count(*) FROM device_state')" 0

# ── 场景2：正常 QoS1 ─────────────────────────────────────────────
info "场景2：dev-A 正常发送 3 条 QoS1"
./bin/device -mqtt "$MQTT" -id dev-A -boot 1 -count 3 -interval 80ms >/dev/null
wait_metric inserted 3 || true
check "events=3" "$(psql_exec 'SELECT count(*) FROM events WHERE device_id=$$dev-A$$ AND boot_gen=1')" 3
check "当前 seq" "$(state_field dev-A current_seq)" 3
check "在线 online=true" "$(curl -s http://127.0.0.1:${HTTP_PORT}/state | python3 -c "import sys,json;print([x['online'] for x in json.load(sys.stdin)['devices'] if x['device_id']=='dev-A'][0])")" True

# ── 场景3：设备重启，序号空间不混淆 ───────────────────────────────
info "场景3：dev-A boot 1→2，seq 重置；旧boot迟到包不回退在线状态"
./bin/device -mqtt "$MQTT" -id dev-A -boot 2 -count 2 -interval 80ms >/dev/null
wait_metric inserted 5 || true
check "events 总=5" "$(psql_exec 'SELECT count(*) FROM events WHERE device_id=$$dev-A$$')" 5
check "当前 boot=2" "$(state_field dev-A current_boot_gen)" 2
check "当前 seq=2" "$(state_field dev-A current_seq)" 2
./bin/inject -mqtt "$MQTT" gap dev-A 1 1 >/dev/null   # 合法但旧boot的重复/迟到样本
sleep 1
check "旧boot包后 boot 不回退" "$(state_field dev-A current_boot_gen)" 2
check "旧boot包后 seq 不回退" "$(state_field dev-A current_seq)" 2
check "业务去重计数(dup)>=1" "$( [ "$(metric duplicates)" -ge 1 ] && echo true || echo false)" true

# ── 场景4：非法 payload 隔离不阻塞他人 ────────────────────────────
info "场景4：dev-B 坏JSON+坏签名进隔离区；dev-C 同刻正常入库"
./bin/inject -mqtt "$MQTT" badjson dev-B >/dev/null
./bin/inject -mqtt "$MQTT" badsig  dev-B >/dev/null
./bin/device -mqtt "$MQTT" -id dev-C -boot 1 -count 2 -interval 80ms >/dev/null
wait_metric quarantined 2 || true
check "隔离区=2" "$(psql_exec 'SELECT count(*) FROM quarantine')" 2
check "dev-B 零 event" "$(psql_exec 'SELECT count(*) FROM events WHERE device_id=$$dev-B$$')" 0
check "dev-C 未被阻塞,2条入库" "$(psql_exec 'SELECT count(*) FROM events WHERE device_id=$$dev-C$$')" 2

# ── 场景5：业务提交失败→回滚不ACK→重投递恰好一次 ─────────────────
info "场景5：arm 提交故障；回滚不留痕且不ACK；清故障后重投递恰好1条"
curl -s -X POST "http://127.0.0.1:${HTTP_PORT}/faults/arm?device=dev-A" >/dev/null
./bin/device -mqtt "$MQTT" -id dev-A -boot 2 -seq-start 3 -count 1 >/dev/null
sleep 2.5
check "故障期间 event 不入库" "$(psql_exec 'SELECT count(*) FROM events WHERE device_id=$$dev-A$$ AND boot_gen=2 AND seq=3')" 0
check "故障期间 raw 也不入库(整事务回滚)" "$(psql_exec "SELECT count(*) FROM raw_messages WHERE device_id='dev-A' AND kind='event' AND payload->>'boot_gen'='2' AND payload->>'seq'='3'")" 0
ok "$( [ "$(metric tx_failures)" -gt 0 ] && echo true || echo false)" "tx_failures>0（确实未ACK并触发重连）"
curl -s -X POST "http://127.0.0.1:${HTTP_PORT}/faults/clear?device=dev-A" >/dev/null
wait_sql "SELECT count(*) FROM events WHERE device_id='dev-A' AND boot_gen=2 AND seq=3" 1 || true
check "重投递后业务行恰好1" "$(psql_exec 'SELECT count(*) FROM events WHERE device_id=$$dev-A$$ AND boot_gen=2 AND seq=3')" 1

# ── 场景6：提交成功但ACK丢失→业务去重 ────────────────────────────
info "场景6：arm 一次性 dropACK；提交后ACK丢失；重投递命中业务去重"
curl -s -X POST "http://127.0.0.1:${HTTP_PORT}/faults/dropack?device=dev-A" >/dev/null
./bin/device -mqtt "$MQTT" -id dev-A -boot 2 -seq-start 4 -count 1 >/dev/null
wait_metric ack_dropped 1 || true
wait_sql "SELECT count(*) FROM raw_messages WHERE device_id='dev-A' AND kind='dup' AND payload->>'boot_gen'='2' AND payload->>'seq'='4'" 1 || true
check "业务行恰好1(去重)" "$(psql_exec 'SELECT count(*) FROM events WHERE device_id=$$dev-A$$ AND boot_gen=2 AND seq=4')" 1
check "dup 审计行=1 且 dup_flag=t" "$(psql_exec "SELECT count(*) FROM raw_messages WHERE device_id='dev-A' AND kind='dup' AND dup_flag AND payload->>'boot_gen'='2' AND payload->>'seq'='4'")" 1

# ── 场景7：kill -9 崩溃，持久会话重投递 ──────────────────────────
info "场景7：arm 故障→发1条→kill -9→重启(sessionPresent=true)→未ACK消息重投恰好1条"
curl -s -X POST "http://127.0.0.1:${HTTP_PORT}/faults/arm?device=dev-A" >/dev/null
./bin/device -mqtt "$MQTT" -id dev-A -boot 2 -seq-start 5 -count 1 >/dev/null
sleep 2
check "崩溃前不入库" "$(psql_exec 'SELECT count(*) FROM events WHERE device_id=$$dev-A$$ AND boot_gen=2 AND seq=5')" 0
stop_consumer
start_consumer "$CID"
ok "$(grep -q 'sessionPresent=true' /tmp/acc-consumer.log && echo true || echo false)" "重启后 sessionPresent=true（broker 持久会话保住订阅与未ACK消息）"
# 故障集是进程内存态，崩溃重启后已清空——这是设计预期：重投递随即正常提交。
wait_sql "SELECT count(*) FROM events WHERE device_id='dev-A' AND boot_gen=2 AND seq=5" 1 || true
check "恢复后 seq5 恰好1条" "$(psql_exec 'SELECT count(*) FROM events WHERE device_id=$$dev-A$$ AND boot_gen=2 AND seq=5')" 1
ok "$(grep -Eq 'COMMITTED.*boot=2 seq=5.*dup=true' /tmp/acc-consumer.log && echo true || echo false)" "重投递带 DUP=1 且只生效一次"

info "汇总：最终计数"
curl -s "http://127.0.0.1:${HTTP_PORT}/metrics"; echo
echo
if [ "$FAIL" = 0 ]; then grn "全部 ${PASS} 项验收通过"; else red "${FAIL} 项失败，${PASS} 项通过"; fi
echo "consumer 日志: /tmp/acc-consumer.log"
exit "$FAIL"
