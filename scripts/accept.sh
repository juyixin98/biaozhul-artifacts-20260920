#!/usr/bin/env bash
# 一键验收：在本地启动 uvicorn，跑通健康检查、审计（含冲突/非正定/缺失协方差）、
# 真实 Ed25519 签名验签、防篡改、Monte Carlo 自检，最后关闭服务。
#
# 用法: ./scripts/accept.sh
set -euo pipefail

cd "$(dirname "$0")/.."
PY=.venv/bin/python
PORT="${PORT:-8791}"
BASE="http://127.0.0.1:${PORT}"

echo "==> [0/5] 自动化测试 (pytest)"
"$PY" -m pytest tests/ -q

echo "==> [1/5] 启动服务 (uvicorn :${PORT})"
"$PY" -m uvicorn app.main:app --host 127.0.0.1 --port "${PORT}" \
  --log-level warning &
SERVER_PID=$!
trap 'kill ${SERVER_PID} 2>/dev/null || true' EXIT

# 等待服务就绪
for i in $(seq 1 50); do
  if curl -sf "${BASE}/health" >/dev/null 2>&1; then break; fi
  sleep 0.2
done

echo "==> [2/5] 健康检查"
curl -s "${BASE}/health" | "$PY" -m json.tool

echo "==> [3/5] 正常链审计（独立声明）+ 签名信封"
RESP=$(curl -s -X POST "${BASE}/audit" \
  -H 'Content-Type: application/json' \
  --data @examples/audit_chain.json)
echo "$RESP" | "$PY" -c '
import json,sys
r=json.load(sys.stdin)
assert r["algorithm"]=="ed25519" and r["signature"]
res=r["result"]
print("  overall:", res["overall"]["status"], "| nodes:", res["overall"]["n_nodes"])
print("  key_id:", r["key_id"])
print("  sha256:", r["signed_payload_sha256"])
open("/tmp/audit_resp.json","w").write(json.dumps(r))
'

echo "==> [3b] 闭环冲突示例"
curl -s -X POST "${BASE}/audit" -H 'Content-Type: application/json' \
  --data @examples/loop_conflict.json | "$PY" -c '
import json,sys
r=json.load(sys.stdin)["result"]
l=r["loops"][0]
print("  conflict:", l["conflict"], "| chi2=%.3f p=%.2e"%(l["mahalanobis_sq"],l["p_value"]))
print("  evidence:", l["evidence_path"])
assert l["conflict"]
'

echo "==> [3c] 缺失协方差（左扰动约定）-> unknown，不判假冲突"
curl -s -X POST "${BASE}/audit" -H 'Content-Type: application/json' \
  --data @examples/missing_covariance.json | "$PY" -c '
import json,sys
r=json.load(sys.stdin)["result"]
l=r["loops"][0]
print("  status:", l["covariance_status"], "| p:", l["p_value"], "| conflict:", l["conflict"])
assert l["covariance_status"]=="unknown" and l["conflict"] is False
'

echo "==> [3d] 非正定协方差 -> 422 + 最短证据"
printf '%s' '{
  "root_frame":"a","correlation_policy":"independent","edges":[{
    "id":"e1","parent":"a","child":"b","version":"v1",
    "transform":{"translation":[0,0,0],"rotation":[1,0,0,0]},
    "covariance":[[-1,0,0,0,0,0],[0,1,0,0,0,0],[0,0,1,0,0,0],[0,0,0,1,0,0],[0,0,0,0,1,0],[0,0,0,0,0,1]]
  }]}' | curl -s -o /tmp/bad.json -w "  HTTP %{http_code}\n" \
  -X POST "${BASE}/audit" -H 'Content-Type: application/json' --data @-
"$PY" -c '
import json; d=json.load(open("/tmp/bad.json"))
i=d["error"]["issues"][0]
print("  code:",i["code"],"| evidence:",i["evidence_path"])
assert i["code"]=="COVARIANCE_NOT_PSD"
'

echo "==> [4/5] 真实 Ed25519 验签 + 防篡改"
"$PY" - "$BASE" <<'PYEOF'
import json, sys, urllib.request
base=sys.argv[1]
r=json.load(open("/tmp/audit_resp.json"))
def post(path, obj):
    req=urllib.request.Request(base+path, data=json.dumps(obj).encode(),
        headers={"Content-Type":"application/json"})
    try:
        with urllib.request.urlopen(req) as resp: return resp.status, json.load(resp)
    except urllib.error.HTTPError as e: return e.code, json.load(e)
st, ok = post("/verify", {"public_key":r["public_key"],"signature":r["signature"],"result":r["result"]})
print("  verify original:", st, ok["valid"]); assert ok["valid"]
tampered=dict(r["result"]); tampered["root_frame"]="HACKED"
st2, bad = post("/verify", {"public_key":r["public_key"],"signature":r["signature"],"result":tampered})
print("  verify tampered:", st2, bad["valid"]); assert bad["valid"] is False
PYEOF

echo "==> [5/5] Monte Carlo 一阶传播自检"
curl -s -X POST "${BASE}/montecarlo/selfcheck?n_samples=20000" | "$PY" -c '
import json,sys
r=json.load(sys.stdin)
for s in r["scenarios"]:
    fe=s["frobenius_relative_error"]
    name=s["scenario"]; st=s["covariance_status"]
    shown = fe if fe is None else round(fe,4)
    print(f"  {name:32s} {st:8s} fro={shown}")
print("  passed:", r["summary"]["passed"])
assert r["summary"]["passed"]
'

echo ""
echo "✅ 验收全部通过"
