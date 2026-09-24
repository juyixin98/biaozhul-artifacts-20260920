#!/usr/bin/env bash
# demo.sh — 用 curl 跑三个验收场景，如实打印每一步的 HTTP 状态码与 JSON。
# 依赖：bash、curl、go（用于启动服务）。启动方式见 README。
set -u

BASE="${BASE:-http://127.0.0.1:8080}"

# 带状态码的 POST：第一行输出 "HTTP <status>"，随后是 JSON 响应体。
post() {
  local path="$1"; shift
  curl -sS -w '\nHTTP %{http_code}\n' \
    -H 'Content-Type: application/json' \
    -X POST "$BASE$path" "$@"
}

banner() {
  printf '\n==================== %s ====================\n' "$1"
}

echo "等待服务在 $BASE 就绪..."
for _ in $(seq 1 50); do
  if curl -fsS "$BASE/health" >/dev/null 2>&1; then break; fi
  sleep 0.1
done

banner "重置环境"
post /reset

banner "场景一：部分写成功后超时 -> 副本恢复 -> 读修复"
echo "--- 1) 宕掉 n2、n3（模拟 2/3 副本不可达）"
post /replicas/down -d '{"node":"n2"}'
post /replicas/down -d '{"node":"n3"}'
echo "--- 2) W=2 写 k=v1：只有 n1 在线，预期 504，quorum_met=false，但 n1 已落地"
post /write -d '{"key":"k","value":"v1","coordinator":"n1"}'
echo "--- 3) 此时读也达不到 R=2，预期 504（不猜测最新值、不修复）"
post /read -d '{"key":"k"}'
echo "--- 4) 恢复 n2、n3（它们带着停机前的旧数据回来）"
post /replicas/up -d '{"node":"n2"}'
post /replicas/up -d '{"node":"n3"}'
echo "--- 5) R=2 读：预期 200，读到 n1 上的 v1，并读修复 n2/n3"
post /read -d '{"key":"k"}'

banner "重置环境"
post /reset

banner "场景二：并发写 -> 读出两个兄弟版本（冲突，非线性一致）-> 客户端消解"
echo "--- 1) 写 A，仅接触 {n1,n2}，协调者 n1"
post /write -d '{"key":"k","value":"A","coordinator":"n1","nodes":["n1","n2"]}'
echo "--- 2) 写 B，仅接触 {n2,n3}，协调者 n2（与 A 无因果关系）"
post /write -d '{"key":"k","value":"B","coordinator":"n2","nodes":["n2","n3"]}'
echo "--- 3) R=2 读：预期 409，versions 含 A、B 两个向量钟不可比较的兄弟版本"
RESP=$(curl -sS -H 'Content-Type: application/json' -X POST "$BASE/read" -d '{"key":"k"}')
echo "$RESP" | sed 's/^/  /'
# 用 python3（若有）抽取两个兄弟钟用于 resolve；没有 jq 时退化为手工演示说明。
CLOCKS=$(echo "$RESP" | python3 -c '
import json,sys
vs=json.load(sys.stdin)["versions"]
print(json.dumps([v["clock"] for v in vs]))
' 2>/dev/null || true)
if [ -n "$CLOCKS" ]; then
  echo "--- 4) 客户端裁决（例如合并语义）：/resolve 以两个兄弟钟为父写 winner"
  python3 - "$BASE" "$CLOCKS" <<'PY'
import json,sys,urllib.request
base, clocks = sys.argv[1], json.loads(sys.argv[2])
body=json.dumps({"key":"k","value":"winner","coordinator":"n3","siblings":clocks}).encode()
req=urllib.request.Request(base+"/resolve",data=body,headers={"Content-Type":"application/json"})
print(urllib.request.urlopen(req).read().decode())
PY
  echo "--- 5) 再读：预期 200，只剩 winner（旧兄弟仍留在副本历史中）"
  post /read -d '{"key":"k"}'
else
  echo "!! 未找到 python3，跳过自动 resolve；可手工把上面 versions[].clock 填入 /resolve"
fi

banner "重置环境"
post /reset

banner "场景三：副本恢复 + 反熵修复（/repair）"
echo "--- 1) 宕掉 n3"
post /replicas/down -d '{"node":"n3"}'
echo "--- 2) W=2 写（n1,n2 成功，n3 错过）"
post /write -d '{"key":"k","value":"v1","coordinator":"n1"}'
echo "--- 3) 恢复 n3（其数据为空/过期），立刻全量反熵修复"
post /replicas/up -d '{"node":"n3"}'
post /repair -d '{"key":"k"}'
echo "--- 4) 查看各副本内部状态：三个副本应持有同一版本"
curl -sS "$BASE/state?key=k" | sed 's/^/  /'

banner "附加对照：W+R<=N 时读写法定人数可能不相交 -> 允许读旧值"
post /reset
echo "--- 切到 W=1,R=1（W+R=2 <= N=3）"
post /config -d '{"w":1,"r":1}'
echo "--- 写只落在 n1"
post /write -d '{"key":"x","value":"only-n1","coordinator":"n1","nodes":["n1"]}'
echo "--- 只读 n2：预期 404（读集合与写集合不相交）——这不是线性一致"
post /read -d '{"key":"x","nodes":["n2"],"no_repair":true}'
echo "--- 只读 n1：预期 200"
post /read -d '{"key":"x","nodes":["n1"],"no_repair":true}'
echo "--- 恢复 W=2,R=2"
post /config -d '{"w":2,"r":2}'

echo
echo "演示完成。"
