#!/usr/bin/env bash
# 一键本地启动：PostgreSQL（需本机已运行）+ 迁移 + 目标桩 + 协调器 + 2 个中继。
set -euo pipefail
cd "$(dirname "$0")/.."

DB_NAME="${DB_NAME:-relay_coord_demo}"
export PGPASSWORD="${PGPASSWORD:-}"
DB_URL="postgresql:///${DB_NAME}"

echo "==> 准备数据库 ${DB_NAME}"
psql -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" | grep -q 1 \
  || createdb "${DB_NAME}"

echo "==> 构建"
cargo build

echo "==> 迁移"
DATABASE_URL="${DB_URL}" ./target/debug/relay-coord migrate

echo "==> 启动目标桩 :19090 / 协调器 :18080 / relay-A relay-B"
RUST_LOG=info ./target/debug/relay-coord stub --bind 127.0.0.1:19090 \
  --secret dev-shared-secret --mode normal > /tmp/relay-demo-stub.log 2>&1 &
echo $! > /tmp/relay-demo-stub.pid
RUST_LOG=info DATABASE_URL="${DB_URL}" ./target/debug/relay-coord serve \
  --bind 127.0.0.1:18080 --lease-secs 10 > /tmp/relay-demo-coord.log 2>&1 &
echo $! > /tmp/relay-demo-coord.pid
RUST_LOG=info ./target/debug/relay-coord relay --relay-id relay-A \
  --coordinator http://127.0.0.1:18080 --target http://127.0.0.1:19090 \
  --secret dev-shared-secret --lease-secs 10 > /tmp/relay-demo-a.log 2>&1 &
echo $! > /tmp/relay-demo-a.pid
RUST_LOG=info ./target/debug/relay-coord relay --relay-id relay-B \
  --coordinator http://127.0.0.1:18080 --target http://127.0.0.1:19090 \
  --secret dev-shared-secret --lease-secs 10 > /tmp/relay-demo-b.log 2>&1 &
echo $! > /tmp/relay-demo-b.pid

sleep 1
cat <<EOF

服务已启动：
  协调器  http://127.0.0.1:18080
  目标桩  http://127.0.0.1:19090

入队示例：
  curl -s -X POST http://127.0.0.1:18080/enqueue \\
    -H 'content-type: application/json' \\
    --data @examples/enqueue.json | jq

停止：./scripts/stop-demo.sh
日志：/tmp/relay-demo-{stub,coord,a,b}.log
EOF
