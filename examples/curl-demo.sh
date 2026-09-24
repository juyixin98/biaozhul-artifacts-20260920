#!/usr/bin/env bash
# 端到端演示：健康检查、策略查询、六个验收/附加场景、内联 envelope 篡改。
# 前置：服务已启动（cargo run --bin provenance-server）。
set -u
BASE="${BASE:-http://127.0.0.1:8080}"

echo "### GET /health"
curl -s "$BASE/health"; echo

echo
echo "### GET /policy"
curl -s "$BASE/policy" | python3 -m json.tool

for s in 01-valid 02-output-replaced 03-missing-material \
         04-cross-builder-reuse 05-forbidden-source 06-untrusted-builder; do
  echo
  echo "### POST /scenarios/$s"
  code=$(curl -s -o /tmp/sc.json -w '%{http_code}' -X POST "$BASE/scenarios/$s")
  echo "HTTP $code"
  python3 -c '
import json
r = json.load(open("/tmp/sc.json"))
rep = r["report"]
print("accepted =", rep["accepted"])
for c in rep["checks"]:
    mark = "PASS" if c["passed"] else "FAIL"
    print("  [%s] %-28s %s" % (mark, c["check"], c["code"]))
'
done

echo
echo "### POST /verify（内联合法 envelope + base64 产物）"
python3 - "$BASE" <<'EOF'
import base64, json, sys, urllib.request
base = sys.argv[1]
env = json.load(open("fixtures/proofs/valid.json"))
art = open("fixtures/artifacts/app-v1.0.0.tar.gz", "rb").read()
req = urllib.request.Request(
    base + "/verify",
    data=json.dumps({
        "envelope": env,
        "artifact_base64": base64.b64encode(art).decode(),
        "actual_builder_id": "pkg:generic/ci-builder@v2",
    }).encode(),
    headers={"Content-Type": "application/json"},
)
print(urllib.request.urlopen(req).read().decode())
EOF
