#!/usr/bin/env bash
# 运行全部测试:单元测试 + 集成测试(真实 Mosquitto + PostgreSQL)
set -euo pipefail
cd "$(dirname "$0")/.."

./scripts/start_broker.sh

echo "== 单元测试 =="
go test ./... -count=1

echo "== 集成测试(真实 broker + 真实数据库) =="
go test -tags=integration ./internal/telemetry/ -count=1 -v -timeout 120s
