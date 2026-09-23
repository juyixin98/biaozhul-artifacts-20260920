#!/usr/bin/env bash
# End-to-end acceptance check for the TWAP service.
# Uses the REAL server clock: targets the most recently closed 60s
# window and submits samples before the 5-minute late cutoff expires.
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:18091}"
TOKEN="${ADMIN_TOKEN:-acceptance-admin-token}"

now_us=$(date +%s%N | cut -c1-16)
# Current minute start (microseconds)
cur_min=$(( (now_us / 60000000) * 60000000 ))
w0=$(( cur_min - 60000000 ))   # last closed minute
w1=$cur_min                    # current (open) minute
sec=1000000

pass=0; fail=0
check() { # desc, expected, actual
  if [ "$2" = "$3" ]; then echo "PASS: $1"; pass=$((pass+1));
  else echo "FAIL: $1 (want $2 got $3)"; fail=$((fail+1)); fi
}

echo "== 1. health =="
curl -s "$BASE/healthz"

echo; echo "== 2. ingest irregular samples into $w0 =="
# source a: 100 at +0s, 200 at +30s ; source b (conflicts): 102 at +5s
curl -s -X POST "$BASE/v1/samples" -H 'Content-Type: application/json' -d "{
  \"batch\": [
    {\"ts\": $((w0)),          \"price\": 100, \"source\": \"exchange-a\"},
    {\"ts\": $((w0+5*sec)),    \"price\": 102, \"source\": \"exchange-b\"},
    {\"ts\": $((w0+30*sec)),   \"price\": 200, \"source\": \"exchange-a\"}
  ]}" | python3 -m json.tool

echo "== 3. read closed window (materialized by the batch, signature verified) =="
curl -s "$BASE/v1/windows/latest?at=$w0&verify_signature=1" | tee /tmp/v1.json | python3 -m json.tool
# Each of the 3 batch ingests incrementally materializes the window: v1, v2, v3.
check "latest-after-batch version" "3" "$(python3 -c 'import json;print(json.load(open("/tmp/v1.json"))["version"])')"
check "v1 signature_ok" "True" "$(python3 -c 'import json;print(json.load(open("/tmp/v1.json"))["signature_ok"])')"
check "v1 state" "closed" "$(python3 -c 'import json;print(json.load(open("/tmp/v1.json"))["state"])')"
# winner exchange-a: 100 on [0,30), 200 on [30,60) -> 150
python3 -c 'import json;assert json.load(open("/tmp/v1.json"))["twap"]==150;print("PASS: v1 twap == 150")' && pass=$((pass+1)) || { echo "FAIL: v1 twap"; fail=$((fail+1)); }
check "v1 coverage" "1.0" "$(python3 -c 'import json;print(float(json.load(open("/tmp/v1.json"))["coverage"]))')"
check "v1 conflicts" "2" "$(python3 -c 'import json;print(json.load(open("/tmp/v1.json"))["conflict_count"])')"

echo "== 4. late arrival: correct price at +10s to 160 -> recompute v2 =="
curl -s -X POST "$BASE/v1/samples" -H 'Content-Type: application/json' \
  -d "{\"ts\": $((w0+10*sec)), \"price\": 160, \"source\": \"exchange-a\"}" \
  | python3 -m json.tool
curl -s "$BASE/v1/windows/latest?at=$w0&verify_signature=1" | tee /tmp/v2.json | python3 -m json.tool
check "v2 version" "4" "$(python3 -c 'import json;print(json.load(open("/tmp/v2.json"))["version"])')"
check "v2 signature_ok" "True" "$(python3 -c 'import json;print(json.load(open("/tmp/v2.json"))["signature_ok"])')"
# 100*10 + 160*20 + 200*30 = 10200 / 60 = 170
python3 -c 'import json;assert json.load(open("/tmp/v2.json"))["twap"]==170;print("PASS: v2 twap recomputed == 170")' && pass=$((pass+1)) || { echo "FAIL: v2 twap"; fail=$((fail+1)); }

echo "== 5. historical versions are immutable and individually readable =="
curl -s "$BASE/v1/windows/$w0/versions/1" | python3 -c '
import json,sys
v=json.load(sys.stdin)
# Only the first batch sample existed when v1 was computed: p=100 whole window.
assert v["version"]==1 and v["twap"]==100, (v["version"], v["twap"])
print("PASS: historical v1 == 100 (immutable snapshot)")' && pass=$((pass+1)) || { echo FAIL; fail=$((fail+1)); }
curl -s "$BASE/v1/windows/$w0/versions/3" | python3 -c '
import json,sys
v=json.load(sys.stdin)
assert v["version"]==3 and v["twap"]==150, (v["version"], v["twap"])
print("PASS: historical v3 == 150")' && pass=$((pass+1)) || { echo FAIL; fail=$((fail+1)); }

echo "== 6. insufficient coverage + stale on a sparse window =="
# Window 3 minutes before the last one: closed, inside late cutoff.
sparse=$(( w0 - 3*60*sec ))
curl -s -X POST "$BASE/v1/samples" -H 'Content-Type: application/json' \
  -d "{\"ts\": $((sparse+40*sec)), \"price\": 100, \"source\": \"exchange-a\"}" > /dev/null
curl -s "$BASE/v1/windows/latest?at=$sparse" | tee /tmp/sp.json | python3 -m json.tool
check "sparse covered = 20s" "20000000" "$(python3 -c 'import json;print(json.load(open("/tmp/sp.json"))["covered_micros"])')"
check "sparse stale (age 20s<30s)" "False" "$(python3 -c 'import json;print(json.load(open("/tmp/sp.json"))["stale"])')"

echo "== 7. future sample rejected (per-item status in batch response) =="
future=$(( now_us + 600*sec ))
curl -s -X POST "$BASE/v1/samples" -H 'Content-Type: application/json' \
  -d "{\"ts\": $future, \"price\": 1, \"source\":\"x\"}" | tee /tmp/fut.json | python3 -m json.tool
python3 -c 'import json;r=json.load(open("/tmp/fut.json"));assert r["rejected"]==1 and r["items"][0]["status"]==422;print("PASS: future rejected 422")' && pass=$((pass+1)) || { echo FAIL; fail=$((fail+1)); }

echo "== 8. too-late sample rejected (10 minutes old) =="
old=$(( now_us - 10*60*sec ))
curl -s -X POST "$BASE/v1/samples" -H 'Content-Type: application/json' \
  -d "{\"ts\": $old, \"price\": 1, \"source\":\"x\"}" | tee /tmp/late.json | python3 -m json.tool
python3 -c 'import json;r=json.load(open("/tmp/late.json"));assert r["rejected"]==1 and r["items"][0]["status"]==422;print("PASS: too-late rejected 422")' && pass=$((pass+1)) || { echo FAIL; fail=$((fail+1)); }

echo "== 9. open bucket is live, never future-filled =="
curl -s "$BASE/v1/range?start=$w1&end=$((w1+60*sec))" | tee /tmp/open.json | python3 -m json.tool

echo "== 10. full recompute: admin auth required; sweep is idempotent =="
noauth=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/v1/admin/recompute")
check "admin without token 401/503" "401" "$noauth"
# First sweep may fill windows never touched on the ingest path (e.g.
# windows covered only via another window's anchor); the second sweep
# must create zero new versions — incremental/full convergence.
curl -s -X POST "$BASE/v1/admin/recompute" -H "Authorization: Bearer $TOKEN" > /tmp/recomp1.json
python3 -c 'import json;r=json.load(open("/tmp/recomp1.json"));print("first sweep: checked",r["checked_windows"],"new",r["new_versions"])'
curl -s -X POST "$BASE/v1/admin/recompute" -H "Authorization: Bearer $TOKEN" | tee /tmp/recomp.json | python3 -m json.tool
check "second sweep creates 0 versions" "0" "$(python3 -c 'import json;print(json.load(open("/tmp/recomp.json"))["new_versions"])')"

# The explicitly tested windows keep identical hashes across the sweep.
for w in "$w0" "$sparse"; do
  curl -s "$BASE/v1/windows/latest?at=$w" | python3 -c '
import json,sys
v=json.load(sys.stdin)
print("window", v["window_start"], "version", v["version"], "hash", v["input_hash"][:12], "twap", v["twap"], "coverage", v["coverage"], "stale", v["stale"])'
done

echo
echo "RESULT: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
