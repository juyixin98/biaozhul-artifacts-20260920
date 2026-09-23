#!/usr/bin/env bash
# 全碰撞容量错误演示：--hash const 让所有键散列相同，桶满后分裂永远无法
# 分离记录，第 (cap+1) 个键得到明确的 507 Insufficient Storage。
set -u
BASE="${EHINDEX_BASE:-http://127.0.0.1:8081}"

echo "服务应以全碰撞哈希启动，例如："
echo "  cargo run -- --addr 127.0.0.1:8081 --file /tmp/collision.db --cap 2 --hash const"
echo

for k in a b c; do
  echo "-- put $k --"
  curl -s -w "\nHTTP %{http_code}\n" -X POST "$BASE/put" \
    -H 'Content-Type: application/json' \
    -d "{\"key\":\"$k\",\"value\":\"v-$k\"}"
  echo
done

echo "== 尽管报错，已提交数据完好 =="
curl -s -X POST "$BASE/get" -H 'Content-Type: application/json' -d '{"key":"a"}'; echo
curl -s "$BASE/stats"; echo
