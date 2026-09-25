#!/usr/bin/env bash
# 端到端 curl 演示：启动本地服务 -> 发布策略 -> 导出 -> 复验。
# 依赖：python3（标准库即可完成请求，本脚本用 python3 发 JSON）。
set -euo pipefail

cd "$(dirname "$0")/.."
DATA_DIR="${MDE_DATA_DIR:-/tmp/mde-curl-demo}"
PORT="${MDE_PORT:-8390}"
rm -rf "$DATA_DIR"

PYTHONPATH=src python3 -m mde.cli --data-dir "$DATA_DIR" serve --port "$PORT" &
SRV=$!
trap 'kill $SRV 2>/dev/null || true' EXIT
sleep 1

BASE="http://127.0.0.1:$PORT"

echo "== 发布策略 =="
FP=$(python3 - "$BASE" <<'PY'
import json, sys, urllib.request
base = sys.argv[1]
doc = json.load(open("examples/policy.json"))
req = urllib.request.Request(base + "/policies", data=json.dumps(doc).encode(),
                             headers={"Content-Type": "application/json"})
resp = json.load(urllib.request.urlopen(req))
print(resp["policy_fingerprint"])
PY
)
echo "policy_fingerprint=$FP"

echo "== 导出（analytics，固定到 v1 指纹） =="
python3 - "$BASE" "$FP" <<'PY'
import json, sys, urllib.request
base, fp = sys.argv[1], sys.argv[2]
records = json.load(open("examples/records.json"))
req = {"purpose": "analytics", "policy_fingerprint": fp, "records": records}
r = urllib.request.Request(base + "/exports", data=json.dumps(req).encode(),
                           headers={"Content-Type": "application/json"})
pkg = json.load(urllib.request.urlopen(r))
json.dump(pkg, open("/tmp/mde-curl-demo-package.json", "w"), indent=2, ensure_ascii=False)
print(json.dumps(pkg["output"][0], indent=2, ensure_ascii=False))
PY

echo "== 复验导出包 =="
python3 - "$BASE" <<'PY'
import json, sys, urllib.request
base = sys.argv[1]
pkg = json.load(open("/tmp/mde-curl-demo-package.json"))
req = urllib.request.Request(base + "/verify",
    data=json.dumps({"package": pkg}).encode(),
    headers={"Content-Type": "application/json"})
rep = json.load(urllib.request.urlopen(req))
print("overall_passed:", rep["overall_passed"])
for c in rep["checks"]:
    print(" -", c["check"], c["status"])
PY
