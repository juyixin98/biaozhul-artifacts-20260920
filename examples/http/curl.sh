#!/usr/bin/env bash
# SAM 本地 HTTP 服务 curl 调用样例。
# 前置:
#   1) python3 -m sam keygen --private-key key.pem --public-key pub.pem
#   2) python3 -m sam.service --port 18088 --signing-key key.pem
#   3) export PUB_B64="$(base64 -w0 pub.pem)"
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:18088}"

echo "== 健康检查 =="
curl -s "$BASE/healthz"; echo

echo "== 离线签名 =="
curl -s -X POST "$BASE/v1/sign" \
  -H 'Content-Type: application/json' \
  --data @examples/http/sign-request.json | tee /tmp/sam-sign-resp.json
echo

echo "== 离线验证 (信任公钥通过环境变量 PUB_B64 注入) =="
python3 - <<'PYEOF'
import json, os, urllib.request

pub = os.environ.get("PUB_B64")
if not pub:
    raise SystemExit("请先 export PUB_B64=$(base64 -w0 pub.pem)")

sign_req = json.load(open("examples/http/sign-request.json"))
sign_resp = json.load(open("/tmp/sam-sign-resp.json"))
body = {
    "envelope": sign_resp["envelope"],
    "trusted_public_keys": [pub],
    "files": sign_req["files"],
}
req = urllib.request.Request(
    os.environ.get("BASE", "http://127.0.0.1:18088") + "/v1/verify",
    data=json.dumps(body).encode("utf-8"),
    headers={"Content-Type": "application/json"},
    method="POST",
)
with urllib.request.urlopen(req) as resp:
    print(json.dumps(json.loads(resp.read()), ensure_ascii=False, indent=2))
PYEOF
