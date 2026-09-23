#!/usr/bin/env bash
# 本地启动：构建并后台运行 metrics-stub(18081) 与 rollout-api(18082)。
set -euo pipefail
cd "$(dirname "$0")/.."

go build -o bin/metrics-stub ./cmd/metrics-stub
go build -o bin/rollout-api ./cmd/server

export DATABASE_URL="${DATABASE_URL:-postgres://rollout:rollout@localhost:5432/rollout?sslmode=disable}"
export METRICS_URL="${METRICS_URL:-http://localhost:18081}"

./bin/metrics-stub & echo $! > /tmp/rollout-stub.pid
./bin/rollout-api  & echo $! > /tmp/rollout-api.pid

for url in http://localhost:18081/scenarios http://localhost:18082/healthz; do
  for i in $(seq 1 30); do
    curl -sf "$url" >/dev/null 2>&1 && break
    sleep 0.2
  done
done
echo "metrics-stub: http://localhost:18081 (pid $(cat /tmp/rollout-stub.pid))"
echo "rollout-api:  http://localhost:18082 (pid $(cat /tmp/rollout-api.pid))"
echo "停止: kill \$(cat /tmp/rollout-api.pid /tmp/rollout-stub.pid)"
