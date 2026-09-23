#!/usr/bin/env bash
# 手工验收示例:先启动 broker 与 server,再执行本脚本,最后用 SQL 验证。
set -euo pipefail
cd "$(dirname "$0")/.."
BROKER=${MQTT_BROKER_HOST:-127.0.0.1}
PUB="mosquitto_pub -h $BROKER -q 1"

echo "1) 正常采样(dev-001 第1代 seq1)"
$PUB -t devices/dev-001/telemetry -f examples/valid.json

echo "2) 同一业务事件再发一遍(模拟 QoS1 重复投递)→ 业务只记一次"
$PUB -t devices/dev-001/telemetry -f examples/valid.json

echo "3) 设备重启:第2代,序号重新从 1 计"
$PUB -t devices/dev-001/telemetry -m \
  '{"device_id":"dev-001","boot_gen":2,"seq":1,"sampled_at":"2026-09-23T10:05:00Z","temperature":22.0,"humidity":0.5}'

echo "4) 旧代次迟到的消息 → 落库但不回退设备状态"
$PUB -t devices/dev-001/telemetry -m \
  '{"device_id":"dev-001","boot_gen":1,"seq":2,"sampled_at":"2026-09-23T10:00:10Z","temperature":21.6,"humidity":0.46}'

echo "5) 非法载荷 → 隔离区"
$PUB -t devices/dev-002/telemetry -f examples/invalid.json

echo "6) 保留消息 → 不得刷新在线状态"
$PUB -t devices/dev-003/telemetry -r -m \
  '{"device_id":"dev-003","boot_gen":1,"seq":1,"sampled_at":"2026-09-23T09:00:00Z","temperature":19.0,"humidity":0.4}'

sleep 1
echo
echo "== 验证 SQL =="
echo "psql postgres://mqtt:mqtt_secret@127.0.0.1:5432/mqtt_telemetry -c \\"
echo "  'SELECT device_id,boot_gen,seq,retained_replay,stale_generation FROM samples ORDER BY id; SELECT * FROM devices; SELECT count(*) FROM quarantine;'"
