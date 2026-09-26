#!/usr/bin/env bash
# 请求样例：对运行中的服务依次执行验收场景。
# 用法: ./requests.sh [base-url]   默认 http://localhost:8080
set -euo pipefail
B="${1:-http://localhost:8080}"

echo '== 1. 元信息（含时区数据库版本） =='
curl -s "$B/api/meta"; echo

echo '== 2. 注册三个优先级版本 =='
curl -s -X PUT "$B/api/versions" -d '{"id":"v1","priority":1}'; echo
curl -s -X PUT "$B/api/versions" -d '{"id":"v2","priority":2}'; echo
curl -s -X PUT "$B/api/versions" -d '{"id":"v3","priority":3}'; echo

echo '== 3. 写入规则（含端点相接与完全覆盖） =='
curl -s -X PUT "$B/api/rules" -d '{"id":"r1","versionId":"v1","start":0,"end":12,"label":"baseline"}'; echo
curl -s -X PUT "$B/api/rules" -d '{"id":"r2","versionId":"v2","start":2,"end":10,"label":"mid-layer"}'; echo
curl -s -X PUT "$B/api/rules" -d '{"id":"r3","versionId":"v3","start":4,"end":6,"label":"top-layer"}'; echo
curl -s -X PUT "$B/api/rules" -d '{"id":"r4","versionId":"v1","start":12,"end":15,"label":"adjacent"}'; echo

echo '== 4. 计算最终生效片段（不重叠、保留来源版本） =='
curl -s -X POST "$B/api/compute" -d '{}'; echo

echo '== 5. 单点查询 =='
curl -s -X POST "$B/api/query" -d '{"point":5}'; echo
curl -s -X POST "$B/api/query" -d '{"point":11}'; echo

echo '== 6. 相同优先级冲突被拒绝（HTTP 409） =='
curl -s -w '\nHTTP %{http_code}\n' -X PUT "$B/api/rules" -d '{"id":"r5","versionId":"v1","start":3,"end":5}'

echo '== 7. 删除版本 v2 后重新计算 =='
curl -s -X DELETE "$B/api/versions/v2"; echo
curl -s -X POST "$B/api/compute" -d '{}'; echo
