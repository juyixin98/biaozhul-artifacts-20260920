#!/usr/bin/env bash
# 停止本地 Mosquitto
pkill -f "mosquitto -c .*mosquitto.conf" && echo "mosquitto 已停止" || echo "mosquitto 未在运行"
