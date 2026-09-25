#!/usr/bin/env bash
# Request samples against a running logmerge server.
# Usage: ./examples/requests.sh [host:port]
set -euo pipefail
ADDR="${1:-127.0.0.1:8080}"

echo "== health =="
curl -s "$ADDR/healthz"; echo

echo "== ingest: interleaved stack traces from two sources =="
curl -s -X POST "$ADDR/ingest" -H 'Content-Type: application/json' -d '[
  {"source":"java-app","line":"2026-09-24 10:00:00 ERROR NullPointerException in OrderService"},
  {"source":"go-worker","line":"2026-09-24 10:00:00 INFO panic: runtime error: index out of range"},
  {"source":"java-app","line":"\tat com.example.OrderService.submit(OrderService.java:42)"},
  {"source":"go-worker","line":"\tgoroutine 17 [running]:"},
  {"source":"java-app","line":"\tat com.example.Main.main(Main.java:10)"},
  {"source":"go-worker","line":"\tmain.processQueue()"},
  {"source":"java-app","line":"2026-09-24 10:00:01 INFO order flow recovered"},
  {"source":"go-worker","line":"2026-09-24 10:00:01 INFO worker restarted"}
]'; echo

echo "== ingest: single object (no array) =="
curl -s -X POST "$ADDR/ingest" -H 'Content-Type: application/json' \
  -d '{"source":"java-app","line":"2026-09-24 10:05:00 WARN single line"}'; echo

echo "== query: all entries =="
curl -s "$ADDR/logs"; echo

echo "== query: one source, newest 10 =="
curl -s "$ADDR/logs?source=java-app&limit=10"; echo
