#!/usr/bin/env bash
# Request samples against a locally running server (default 127.0.0.1:8080).
# Usage: ./samples/requests.sh [host:port]
set -u
ADDR="${1:-127.0.0.1:8080}"

echo '== 1. basic stream (10 items) =='
curl -sN "http://$ADDR/stream?items=10&item_delay_ms=20" | head -3
echo '...'

echo '== 2. upstream error after 3 items -> trailer record =='
curl -sN "http://$ADDR/stream?items=50&fail_after=3" | tail -2

echo '== 3. oversized item rejected up front (HTTP 413) =='
curl -s -o /dev/stderr -w 'http_status=%{http_code}\n' "http://$ADDR/stream?item_size=99999999" 2>&1 | tail -2

echo '== 4. observability snapshot =='
curl -s "http://$ADDR/stats"
echo
curl -s "http://$ADDR/healthz"
