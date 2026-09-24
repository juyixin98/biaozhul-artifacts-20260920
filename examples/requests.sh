#!/bin/sh
# Request samples for the udpsim HTTP API.
# Start the server first:  go run ./cmd/udpsim -addr :8080
set -eu

HOST="${HOST:-localhost:8080}"

echo "== health =="
curl -s "$HOST/api/health"
echo

echo "== 256 KiB over loopback UDP, drop/dup/reorder + stale packets =="
curl -s "$HOST/api/transfers" -d '{
  "sizeBytes": 262144, "seed": 42,
  "dropRate": 0.10, "dupRate": 0.05, "reorderRate": 0.10,
  "stalePackets": 5, "window": 16, "chunkSize": 1024,
  "rtoMs": 20, "transport": "udp"
}'
echo

echo "== sequence wrap-around (initialSeq = 2^32 - 16), in-memory link =="
curl -s "$HOST/api/transfers" -d '{
  "sizeBytes": 65536, "seed": 7, "dropRate": 0.05,
  "initialSeq": 4294967280, "rtoMs": 20, "transport": "mem"
}'
echo

echo "== invalid config (drop rate 0.99 violates bounded loss) -> HTTP 400 =="
curl -s -w '\nHTTP %{http_code}\n' "$HOST/api/transfers" -d \
  '{"sizeBytes": 1024, "dropRate": 0.99}'
