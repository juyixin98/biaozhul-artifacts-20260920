#!/usr/bin/env bash
# Example: ingest a deterministic multi-source stream into the most
# recently closed 60s window, then read the time-weighted result.
#
# Prices (winner = exchange-a):
#   [w+0s,  w+10s): 100
#   [w+10s, w+30s): 160   <- late correction demonstrates versioning
#   [w+30s, w+60s): 200
# exchange-b disagrees at w+5s (102) -> conflict flagged, deterministic
# lexicographic winner exchange-a.
set -euo pipefail
BASE="${BASE:-http://127.0.0.1:8080}"

now_us=$(date +%s%N | cut -c1-16)
w=$(( (now_us / 60000000) * 60000000 - 60000000 ))  # last closed minute
sec=1000000

post() { curl -s -X POST "$BASE/v1/samples" -H 'Content-Type: application/json' -d "$1"; echo; }

echo "== initial batch =="
post "{\"batch\":[
  {\"ts\": $((w)),        \"price\":100,\"source\":\"exchange-a\"},
  {\"ts\": $((w+5*sec)),  \"price\":102,\"source\":\"exchange-b\"},
  {\"ts\": $((w+30*sec)), \"price\":200,\"source\":\"exchange-a\"}
]}"

echo "== read v-latest: TWAP must be (100*30+200*30)/60 = 150 =="
curl -s "$BASE/v1/windows/latest?at=$w&verify_signature=1" | python3 -m json.tool

echo "== late correction (up to 5 minutes after close): w+10s -> 160 =="
post "{\"ts\": $((w+10*sec)), \"price\":160, \"source\":\"exchange-a\"}"

echo "== read again: (100*10+160*20+200*30)/60 = 170, new version =="
curl -s "$BASE/v1/windows/latest?at=$w&verify_signature=1" | python3 -m json.tool
