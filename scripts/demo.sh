#!/usr/bin/env bash
# 端到端演示：启动服务后运行本脚本。
# 构造 5 个分片（普通/边界相等+NULL/高值/全NULL/统计缺失），
# 跑 EQ 10、EQ 0、GE 4、无过滤四个查询，对照裁剪与全扫描，输出读取字节。
#
# 用法: BASE_URL=http://localhost:8080 scripts/demo.sh
set -euo pipefail
cd "$(dirname "$0")/.."
BASE="${BASE_URL:-http://localhost:8080}"
T="${TABLE:-demo_manual}"

post() { curl -sS -X POST "$BASE$1" -H 'Content-Type: application/json' -d "$2"; }

echo "== 健康检查 =="
curl -sS "$BASE/health"; echo

echo "== shard0: 低值 [1..4] =="
post "/tables/$T/ingest" "$(cat <<'JSON'
{"types":{"id":"LONG","amount":"LONG"},"rows":[
 {"id":1,"amount":1},{"id":2,"amount":2},{"id":3,"amount":3},{"id":4,"amount":4}]}
JSON
)"; echo

echo "== shard1: 边界相等 + NULL（非空值全为 10，另 2 个 NULL）=="
post "/tables/$T/ingest" "$(cat <<'JSON'
{"rows":[
 {"id":5,"amount":10},{"id":6,"amount":10},
 {"id":7,"amount":null},{"id":8,"amount":null}]}
JSON
)"; echo

echo "== shard2: 高值 [100..400] =="
post "/tables/$T/ingest" "$(cat <<'JSON'
{"rows":[
 {"id":9,"amount":100},{"id":10,"amount":200},
 {"id":11,"amount":300},{"id":12,"amount":400}]}
JSON
)"; echo

echo "== shard3: 全 NULL =="
post "/tables/$T/ingest" "$(cat <<'JSON'
{"types":{"id":"LONG","amount":"LONG"},"rows":[
 {"id":13,"amount":null},{"id":14,"amount":null},
 {"id":15,"amount":null},{"id":16,"amount":null}]}
JSON
)"; echo

echo "== shard4: 统计缺失（computeStats=false），物理值 [50..80] =="
post "/tables/$T/ingest" "$(cat <<'JSON'
{"computeStats":false,"rows":[
 {"id":17,"amount":50},{"id":18,"amount":60},
 {"id":19,"amount":70},{"id":20,"amount":80}]}
JSON
)"; echo

QUERY_AGGS='"aggregates":[
  {"alias":"cnt","func":"count"},
  {"alias":"cnt_amount","func":"count_col","column":"amount"},
  {"alias":"total","func":"sum","column":"amount"},
  {"alias":"avg_amount","func":"avg","column":"amount"},
  {"alias":"min_amount","func":"min","column":"amount"},
  {"alias":"max_amount","func":"max","column":"amount"}]'

run_query() {
  local title="$1"; local body="$2"
  echo
  echo "===================================================="
  echo "$title"
  echo "----------------------------------------------------"
  post /query "$body" > /tmp/colscan-demo-out.json
  cat /tmp/colscan-demo-out.json
  echo
  echo "-- 摘要 --"
  python3 - <<'PY'
import json
r=json.load(open('/tmp/colscan-demo-out.json'))
p,f=r['pruned'],r['fullScan']
print(f"一致: {r['consistentWithFullScan']}  节省字节: {r['bytesSaved']} ({r['bytesSavedRatio']}%)")
print(f"裁剪: 命中 {p['matchedRows']} 行, 扫描分片 {p['scannedShards']}/{p['scannedShards']+p['prunedShards']}, "
      f"读取 {p['totalBytesRead']} 字节 (stats {p['statsBytesRead']} + data {p['dataBytesRead']})")
print(f"全扫: 命中 {f['matchedRows']} 行, 扫描分片 {f['scannedShards']}/{f['scannedShards']+f['prunedShards']}, "
      f"读取 {f['totalBytesRead']} 字节")
for s in p['shards']:
    print(f"  shard {s['shard']}: {'扫描' if s['scanned'] else '裁剪'}  {s['reason']}  "
          f"matched={s['matchedRows']} bytes={s['totalBytesRead']}")
print(f"聚合(裁剪): {p['aggregates']}")
PY
}

run_query "查询1: amount EQ 10（边界相等必须命中；全NULL不命中；统计缺失片被强制扫描）" "{
 \"table\":\"$T\",\"filter\":{\"column\":\"amount\",\"op\":\"eq\",\"value\":10},
 $QUERY_AGGS, \"returnRows\":true, \"rowLimit\":10}"

run_query "查询2: amount EQ 0（验收：NULL 绝不能被当成 0，命中应为 0，聚合 SUM/AVG 为 null）" "{
 \"table\":\"$T\",\"filter\":{\"column\":\"amount\",\"op\":\"eq\",\"value\":0},
 $QUERY_AGGS}"

run_query "查询3: amount GE 4（max==4 边界相等片必须扫描；统计缺失片也扫描）" "{
 \"table\":\"$T\",\"filter\":{\"column\":\"amount\",\"op\":\"ge\",\"value\":4},
 $QUERY_AGGS}"

run_query "查询4: 无过滤（count(*)=20, count(amount)=16，SUM=1290，NULL 不计入）" "{
 \"table\":\"$T\", $QUERY_AGGS}"
