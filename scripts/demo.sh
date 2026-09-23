#!/usr/bin/env bash
# 端到端演示：构建并启动本地 TCP 服务，发送全部样例，打印结果。
set -euo pipefail

cd "$(dirname "$0")/.."

ADDR="${ADDR:-127.0.0.1:10053}"
PORT="${PORT:-10053}"
if [ "$ADDR" = "127.0.0.1:10053" ] && [ "$PORT" != "10053" ]; then
  ADDR="127.0.0.1:${PORT}"
fi

echo "== 构建 release 二进制 =="
cargo build --release --bin dns-tcp-server --example client --example gen_samples

echo "== 重新生成样例 =="
cargo run --quiet --release --example gen_samples

echo "== 启动服务 ${ADDR} =="
./target/release/dns-tcp-server "$ADDR" 4096 >/tmp/dns-server.log 2>&1 &
PID=$!
trap 'kill ${PID} 2>/dev/null || true' EXIT

# 等待端口就绪
for _ in $(seq 1 50); do
  if (exec 3<>/dev/tcp/127.0.0.1/"${PORT}") 2>/dev/null; then
    exec 3<&-; exec 3>&-; break
  fi
  sleep 0.05
done

echo "== 对每个样例发一帧 =="
for f in samples/*.bin; do
  echo "----- ${f} -----"
  ./target/release/examples/client "$ADDR" "$f" || true
  echo
done

echo "== 服务端日志 =="
cat /tmp/dns-server.log
