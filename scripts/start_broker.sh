#!/usr/bin/env bash
# 启动本地 Mosquitto(后台),日志与持久化在 ./var/
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p var
if pgrep -f "mosquitto -c .*mosquitto.conf" >/dev/null; then
  echo "mosquitto 已在运行"
  exit 0
fi
mosquitto -c mosquitto.conf -d
sleep 0.5
mosquitto_sub -h 127.0.0.1 -t '$SYS/broker/version' -C 1 -W 3 >/dev/null \
  && echo "mosquitto 已启动: tcp://127.0.0.1:1883" \
  || { echo "mosquitto 启动失败,见 var/mosquitto.log"; exit 1; }
