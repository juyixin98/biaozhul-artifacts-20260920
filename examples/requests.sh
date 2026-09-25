#!/usr/bin/env bash
# boolsearch JSON API 请求样例
# 先启动服务: java -cp out/main boolsearch.Main serve --port 18099 --docs 100 --seed 42
set -euo pipefail
BASE="${BASE:-http://localhost:18099}"

echo '### 1. 健康检查'
curl -s "$BASE/health"; echo

echo '### 2. 布尔查询（优化开，trace 显示交集顺序）'
curl -s -X POST "$BASE/query" -H 'Content-Type: application/json' \
  -d '{"query":"apple AND banana AND NOT grape","optimize":true}'; echo

echo '### 3. 同一查询（优化关，结果应一致）'
curl -s -X POST "$BASE/query" -H 'Content-Type: application/json' \
  -d '{"query":"apple AND banana AND NOT grape","optimize":false}'; echo

echo '### 4. 交集顺序对比（df 差异明显）'
curl -s -X POST "$BASE/query" -d '{"query":"mango AND apple AND kiwi","optimize":true}'; echo
curl -s -X POST "$BASE/query" -d '{"query":"mango AND apple AND kiwi","optimize":false}'; echo

echo '### 5. 纯 NOT（相对固定文档全集求补）'
curl -s -X POST "$BASE/query" -d '{"query":"NOT kiwi"}'; echo

echo '### 6. 未知词'
curl -s -X POST "$BASE/query" -d '{"query":"nosuchterm"}'; echo
curl -s -X POST "$BASE/query" -d '{"query":"NOT nosuchterm"}'; echo

echo '### 7. 解析错误（HTTP 400，保留字符位置）'
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/query" -d '{"query":"apple AND"}'
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/query" -d '{"query":"(apple OR banana"}'

echo '### 8. 新增文档后重查'
curl -s -X POST "$BASE/documents" -d '{"id":1000,"text":"apple banana kiwi"}'; echo
curl -s -X POST "$BASE/query" -d '{"query":"kiwi AND apple"}'; echo

echo '### 9. 删除文档后全集收缩'
curl -s -X DELETE "$BASE/documents/1000"; echo
curl -s -X POST "$BASE/query" -d '{"query":"kiwi AND apple"}'; echo

echo '### 10. 删除不存在文档（HTTP 404）'
curl -s -w '\nHTTP %{http_code}\n' -X DELETE "$BASE/documents/999"

echo '### 11. 当前文档全集 / 重建合成语料'
curl -s "$BASE/documents" | head -c 200; echo
curl -s -X POST "$BASE/corpus" -d '{"docs":50,"seed":7}'; echo
