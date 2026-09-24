#!/usr/bin/env bash
# examples/requests.sh —— “UDP 可靠传输模拟”HTTP 接口请求样例
#
# 用法：
#   ./examples/requests.sh            # 默认 http://localhost:8080
#   BASE=http://localhost:18080 ./examples/requests.sh
#
# 前置：先启动服务  go run ./cmd/server -addr :8080
set -euo pipefail

BASE="${BASE:-http://localhost:8080}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# 造一个 20000 字节的确定性“文件”
head -c 20000 /dev/urandom > "$TMP/file.bin"

line() { printf '\n========== %s ==========\n' "$1"; }

line "0. 接口说明"
curl -s "$BASE/"

line "1. 干净内存链路（无故障）"
curl -s -X POST --data-binary @"$TMP/file.bin" "$BASE/transfer?mode=fake"

line "2. 固定种子 + 丢包/重复/乱序/旧连接报文（有限预算，必然完成）"
curl -s -X POST --data-binary @"$TMP/file.bin" \
  "$BASE/transfer?mode=fake&seed=20260924&loss=1&ackloss=1&dup=1&ackdup=1&reorder=1&hold=3&budget=12"

line "3. 真实 UDP 回环 + 随机故障（概率性，协议收敛）"
curl -s -X POST --data-binary @"$TMP/file.bin" \
  "$BASE/transfer?mode=udp&seed=42&loss=0.2&ackloss=0.25&dup=0.05&reorder=0.1&budget=0"

line "4. 序号回绕：startseq 接近 2^32，小 MSS 跨过回绕边界"
curl -s -X POST --data-binary @"$TMP/file.bin" \
  "$BASE/transfer?mode=fake&seed=7&mss=200&window=4&startseq=4294967290&loss=0.4&ackloss=0.4&budget=0"

line "5. 大文件 1MiB + 观察窗口内存高水位（window=16, mss=1024 => 上限 16384 字节）"
head -c 1048576 /dev/urandom > "$TMP/big.bin"
curl -s -X POST --data-binary @"$TMP/big.bin" \
  "$BASE/transfer?mode=fake&seed=5&mss=1024&window=16&loss=0.2&ackloss=0.2&reorder=0.1&budget=0" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); s=d["sender"]; print(json.dumps({"ok":d["ok"],"size":d["params"]["size"],"maxBufferedPackets":s["MaxBufferedPackets"],"maxBufferedBytes":s["MaxBufferedBytes"],"retransmits":s["Retransmits"],"hashEqual":d["hashes"]["sender"]==d["hashes"]["receiver"]}, indent=2, ensure_ascii=False))'

line "6. 空文件"
curl -s -X POST "$BASE/transfer?mode=fake"

printf '\n完成。\n'
