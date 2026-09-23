#!/usr/bin/env bash
# 端到端演示：启动 tls-observer-server，依次发送全部合成报文样例，
# 再做一次“逐字节分片”发送和一次 openssl 真实 ClientHello 观察。
#
# 用法: scripts/demo.sh [监听端口，默认 127.0.0.1:9443]
#
# 输出：每条连接一行 JSON（stdout）；运行信息（stderr）。
set -euo pipefail

ADDR="${1:-127.0.0.1:9443}"
HOST="${ADDR%:*}"
PORT="${ADDR##*:}"
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
cd "$ROOT"

# 静默超时设短一点，让 openssl 那条“握手无人应答”的连接也能尽快出报告。
./target/release/tls-observer-server "$ADDR" --read-timeout-ms 1500 \
    > "$ROOT/demo_report.jsonl" 2> "$ROOT/demo_server.log" &
SRV=$!
cleanup() { kill "$SRV" 2>/dev/null || true; }
trap cleanup EXIT

# 等监听就绪（轮询日志，不额外发起连接以免污染报告）。
for _ in $(seq 1 50); do
    if grep -q "listening on" "$ROOT/demo_server.log" 2>/dev/null; then
        break
    fi
    sleep 0.1
done

echo "[demo] sending 16 synthetic samples ..." >&2
for f in "$ROOT"/samples/*.bin; do
    python3 "$HERE/feed_sample.py" "$HOST" "$PORT" "$f"
    printf '[demo]   %s sent\n' "$(basename "$f")" >&2
done

echo "[demo] re-sending sample 13 one byte at a time (incremental TCP) ..." >&2
python3 "$HERE/feed_sample.py" "$HOST" "$PORT" \
    "$ROOT/samples/13_valid_for_tiny_tcp_segments.bin" 1 0.001

if command -v openssl >/dev/null 2>&1; then
    echo "[demo] real TLS 1.3 ClientHello via openssl s_client (server will not finish handshake) ..." >&2
    # 观察器只看明文 ClientHello；openssl 收不到 ServerHello 会自行报错退出，属预期。
    timeout 6 openssl s_client -connect "$HOST:$PORT" \
        -servername real-openssl.example.org -alpn h2,http/1.1 \
        -tls1_3 </dev/null >/dev/null 2>"$ROOT/demo_openssl.err" || true
fi

# 等服务端把最后几条连接的报告刷盘。
sleep 0.5
echo "[demo] reports written to demo_report.jsonl:" >&2
cat "$ROOT/demo_report.jsonl"
