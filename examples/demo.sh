#!/usr/bin/env bash
# End-to-end acceptance demo against a locally started server.
# Uses only curl + python3 (for JSON extraction). No production accounts,
# no network beyond 127.0.0.1, all keys generated locally at runtime.
set -euo pipefail

PORT="${PORT:-8091}"
BASE="http://127.0.0.1:${PORT}"

echo ">> starting server on ${BASE}"
python3 -m threshold_shares.cli serve --port "${PORT}" >/tmp/tss-demo-server.log 2>&1 &
SERVER_PID=$!
trap 'kill "${SERVER_PID}" 2>/dev/null || true' EXIT

# wait for readiness
for _ in $(seq 1 50); do
  if curl -fsS "${BASE}/healthz" >/dev/null 2>&1; then break; fi
  sleep 0.1
done

echo
echo "== health =="
curl -fsS "${BASE}/healthz" | python3 -m json.tool

echo
echo "== split: 3-of-5 =="
SPLIT=$(curl -fsS -X POST "${BASE}/v1/split" \
  -H 'Content-Type: application/json' \
  -d '{"threshold":3,"total":5,"secret_text":"correct horse battery staple"}')
echo "${SPLIT}" | python3 -m json.tool | head -20
FP=$(echo "${SPLIT}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["secret_fingerprint"])')
echo "${SPLIT}" > /tmp/tss-demo-split.json

echo
echo "== recover with shares 1,3,5 + expected fingerprint =="
python3 - "${BASE}" "${FP}" <<'PY'
import json, sys, urllib.request
base, fp = sys.argv[1], sys.argv[2]
split = json.load(open("/tmp/tss-demo-split.json"))
req = {"shares": [split["shares"][0], split["shares"][2], split["shares"][4]],
       "expected_fingerprint": fp}
http = urllib.request.Request(base + "/v1/recover",
    data=json.dumps(req).encode(), headers={"Content-Type": "application/json"})
print(json.dumps(json.load(urllib.request.urlopen(http)), indent=2, ensure_ascii=False))
PY

echo
echo "== recover with only 2 shares -> HTTP 400 insufficient_shares =="
curl -sS -o /tmp/tss-demo-err.json -w "HTTP %{http_code}\n" -X POST "${BASE}/v1/recover" \
  -H 'Content-Type: application/json' \
  --data "$(python3 -c 'import json; d=json.load(open("/tmp/tss-demo-split.json")); print(json.dumps({"shares": d["shares"][:2]}))')"
cat /tmp/tss-demo-err.json | python3 -m json.tool

echo
echo "== mixed batch (two shares of this split + one from a 2-of-3 split) -> 400 mixed_batch =="
OTHER=$(curl -fsS -X POST "${BASE}/v1/split" -H 'Content-Type: application/json' \
  -d '{"threshold":2,"total":3,"secret_text":"different secret"}')
curl -sS -o /tmp/tss-demo-err2.json -w "HTTP %{http_code}\n" -X POST "${BASE}/v1/recover" \
  -H 'Content-Type: application/json' \
  --data "$(OTHER=$(echo "${OTHER}") python3 -c '
import json, os
d=json.load(open("/tmp/tss-demo-split.json")); o=json.loads(os.environ["OTHER"])
print(json.dumps({"shares":[d["shares"][0], d["shares"][1], o["shares"][0]]}))')"
cat /tmp/tss-demo-err2.json | python3 -m json.tool

echo
echo "== corrupt share encoding -> 400 invalid_share_encoding =="
curl -sS -o /tmp/tss-demo-err3.json -w "HTTP %{http_code}\n" -X POST "${BASE}/v1/recover" \
  -H 'Content-Type: application/json' \
  --data "$(python3 -c 'import json; d=json.load(open("/tmp/tss-demo-split.json")); print(json.dumps({"shares":[d["shares"][0], "SSS1$@@@garbage", d["shares"][2]]}))')"
cat /tmp/tss-demo-err3.json | python3 -m json.tool

echo
echo "== duplicate x (same share twice) -> 400 duplicate_share_index =="
curl -sS -o /tmp/tss-demo-err4.json -w "HTTP %{http_code}\n" -X POST "${BASE}/v1/recover" \
  -H 'Content-Type: application/json' \
  --data "$(python3 -c 'import json; d=json.load(open("/tmp/tss-demo-split.json")); print(json.dumps({"shares":[d["shares"][0], d["shares"][0], d["shares"][1]]}))')"
cat /tmp/tss-demo-err4.json | python3 -m json.tool

echo
echo ">> demo complete"
