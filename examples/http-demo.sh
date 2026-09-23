#!/usr/bin/env bash
# 端到端 HTTP 演示：启动服务 -> 插入 -> 精确/近似检索 -> 过滤 -> 维度校验 -> 余弦拒绝零向量 -> 删除
# 仅依赖 JDK 与 curl；演示结束自动关闭服务。
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -n "${JAVA_HOME:-}" ]; then JAVA="$JAVA_HOME/bin/java"; else JAVA="${JAVA:-java}"; fi
# 找一个空闲端口（避免与机器上已有服务冲突；可用 PORT=xxxx 覆盖）
if [ -z "${PORT:-}" ]; then
  PORT="$(python3 - <<'PY'
import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
PY
)"
fi
BASE="http://127.0.0.1:$PORT"
echo "(using ephemeral port $PORT)"

echo "### 1) 启动 L2 服务 (metric=L2, port=$PORT)"
"$JAVA" -cp build/classes vecsearch.Main --port "$PORT" --host 127.0.0.1 --metric L2 >/tmp/vec-demo-l2.log 2>&1 &
SRV=$!
trap 'kill $SRV 2>/dev/null || true' EXIT

# 等待端口就绪
for _ in $(seq 1 50); do
  curl -sf "$BASE/healthz" >/dev/null 2>&1 && break
  sleep 0.1
done

echo; echo "### 2) 健康检查"
curl -s "$BASE/healthz"; echo

echo; echo "### 3) 批量插入 4 条二维向量（带标签）"
curl -s -X POST "$BASE/v1/vectors" -H 'Content-Type: application/json' -d '{
  "vectors": [
    {"id":"v0","vector":[0.0,0.0],"filter":{"cat":"red","grp":"a"}},
    {"id":"v1","vector":[1.0,0.0],"filter":{"cat":"blue","grp":"a"}},
    {"id":"v2","vector":[2.0,0.0],"filter":{"cat":"red","grp":"b"}},
    {"id":"v3","vector":[9.0,0.0],"filter":{"cat":"blue","grp":"b"}}
  ]
}'; echo

echo; echo "### 4) 精确基线检索 k=2"
curl -s -X POST "$BASE/v1/search/exact" -H 'Content-Type: application/json' \
  -d '{"vector":[0.2,0.0],"k":2}'; echo

echo; echo "### 5) 近似检索（先训练 IVF，nlist=2）"
curl -s -X POST "$BASE/v1/index/rebuild" -H 'Content-Type: application/json' \
  -d '{"nlist":2,"maxIters":20,"seed":42}'; echo

echo; echo "### 6) 近似检索 nprobe=1（低预算）与 nprobe=2（全预算）"
curl -s -X POST "$BASE/v1/search" -H 'Content-Type: application/json' \
  -d '{"vector":[0.2,0.0],"k":2,"nprobe":1}'; echo
curl -s -X POST "$BASE/v1/search" -H 'Content-Type: application/json' \
  -d '{"vector":[0.2,0.0],"k":2,"nprobe":2}'; echo

echo; echo "### 7) 标签过滤：只取 cat=red（AND 语义）"
curl -s -X POST "$BASE/v1/search/exact" -H 'Content-Type: application/json' \
  -d '{"vector":[0.0,0.0],"k":10,"filter":{"cat":"red"}}'; echo

echo; echo "### 8) 维度不一致 -> 期望 400"
curl -s -o /dev/null -w "http_status=%{http_code}\n" -X POST "$BASE/v1/vectors" \
  -H 'Content-Type: application/json' -d '{"id":"bad","vector":[1.0,2.0,3.0]}'

echo; echo "### 9) 删除 v1，再检索确认不返回"
curl -s -X DELETE "$BASE/v1/vectors/v1"; echo
curl -s -X POST "$BASE/v1/search" -H 'Content-Type: application/json' \
  -d '{"vector":[1.0,0.0],"k":10,"nprobe":2}'; echo

kill $SRV 2>/dev/null || true
trap - EXIT

echo; echo "### 10) 启动 COSINE 服务并验证零向量被拒绝（插入与检索各一次）"
"$JAVA" -cp build/classes vecsearch.Main --port "$PORT" --host 127.0.0.1 --metric COSINE >/tmp/vec-demo-cos.log 2>&1 &
SRV=$!
trap 'kill $SRV 2>/dev/null || true' EXIT
for _ in $(seq 1 50); do
  curl -sf "$BASE/healthz" >/dev/null 2>&1 && break
  sleep 0.1
done
curl -s -X POST "$BASE/v1/vectors" -H 'Content-Type: application/json' \
  -d '{"id":"a","vector":[1.0,0.0]}' >/dev/null
echo -n "insert zero vector -> "
curl -s -X POST "$BASE/v1/vectors" -H 'Content-Type: application/json' \
  -d '{"id":"z","vector":[0.0,0.0]}'; echo
echo -n "zero query         -> "
curl -s -X POST "$BASE/v1/search/exact" -H 'Content-Type: application/json' \
  -d '{"vector":[0.0,0.0],"k":1}'; echo
echo -n "normal cosine query -> "
curl -s -X POST "$BASE/v1/search/exact" -H 'Content-Type: application/json' \
  -d '{"vector":[0.9,0.1],"k":1}'; echo
kill $SRV 2>/dev/null || true
echo; echo "### demo finished"
