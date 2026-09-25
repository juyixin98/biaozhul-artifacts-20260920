#!/usr/bin/env bash
# e2e 验证：启动 ipfrag-server → fraggen 生成（乱序/packet 模式 + fields 模式）
# → 经真实 TCP 重组 → 用 Python 独立复算原始载荷，逐字节 + SHA-256 双重比对。
#
# 用法: scripts/verify_e2e.sh [port]
# 退出码: 0=全部一致；非 0=失败（输出差异详情）。
set -euo pipefail
cd "$(dirname "$0")/.."

PORT="${1:-9077}"
BIN=./target/debug
WORK="$(mktemp -d)"
trap 'pkill -f "ipfrag-server --port $PORT" 2>/dev/null || true; rm -rf "$WORK"' EXIT

echo "[1/4] cargo build"
cargo build --quiet

echo "[2/4] start server on 127.0.0.1:$PORT"
"$BIN/ipfrag-server" --port "$PORT" --ttl-ms 30000 >"$WORK/server.log" 2>&1 &
for _ in $(seq 1 50); do
  grep -q "listening on 127.0.0.1:$PORT" "$WORK/server.log" 2>/dev/null && break
  sleep 0.05
done
grep -q "listening" "$WORK/server.log"

run_case() {
  local label="$1"; shift
  echo "[3/4] case: $label"
  "$BIN/fraggen" "$@" > "$WORK/req.jsonl"
  # 让完成响应携带 payload_hex，用于逐字节比对
  sed -i 's/"op":"frag"/"op":"frag","include_payload":true/' "$WORK/req.jsonl"
  "$BIN/ipfrag-client" --port "$PORT" "$WORK/req.jsonl" > "$WORK/resp.jsonl"
  grep -q '"event":"completed"' "$WORK/resp.jsonl"
  echo "[4/4] independent byte-level verification"
  python3 scripts/_compare_payload.py "$WORK/resp.jsonl" "$SEED" "$LENGTH"
}

# 用例 A：洗牌乱序 + 原始 IPv4 packet_hex 模式（经手写解析器）
SEED=1234 LENGTH=200
run_case "shuffle + packet_hex (seed=$SEED, length=$LENGTH)" \
  --id 7777 --mtu-payload 16 --length "$LENGTH" --seed "$SEED" \
  --order shuffle --mode packet

# 用例 B：逆序 + 显式字段模式
SEED=99 LENGTH=137
run_case "reverse + fields (seed=$SEED, length=$LENGTH)" \
  --id 7778 --mtu-payload 24 --length "$LENGTH" --seed "$SEED" \
  --order reverse

echo ""
echo "E2E OK: reassembled datagrams are byte-identical to original payloads"
