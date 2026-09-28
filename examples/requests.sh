#!/usr/bin/env bash
# 端到端请求样例（本地合成）。前置：先启动服务
#   uvicorn app.asgi:app --host 127.0.0.1 --port 8000
set -euo pipefail
BASE=${BASE:-http://127.0.0.1:8000}
INV='Authorization: Bearer synt-investigator-token'
AUD='Authorization: Bearer synt-auditor-token'
ADM='Authorization: Bearer synt-admin-token'

# 固定测试种子（32 字节，公开合成值；生产请自行生成）
SEED=$(python3 -c "import base64;print(base64.b64encode(b'rct-fixed-seed-v1-A'.ljust(32,b'0')).decode())")

echo '== 1) 健康检查（无需令牌）=='
curl -s "$BASE/healthz" | python3 -m json.tool

echo '== 2) 创建研究（investigator）=='
curl -s -X POST "$BASE/v1/studies" -H "$INV" -H 'Content-Type: application/json' -d "{
  \"study_id\": \"DEMO-1\",
  \"arms\": [{\"arm_id\":\"control\",\"ratio\":1},{\"arm_id\":\"treatment\",\"ratio\":1}],
  \"factors\": [{\"name\":\"center\",\"levels\":[\"C1\",\"C2\",\"C3\"]},
               {\"name\":\"stage\",\"levels\":[\"early\",\"late\"]}],
  \"block_multiple\": 2,
  \"tail_policy\": \"keep_open\",
  \"seed_base64\": \"$SEED\"
}" | python3 -m json.tool

echo '== 3) 登记分配（携带 Idempotency-Key 可安全重试）=='
for i in 0 1 2 3 4; do
  if [ $((i % 2)) = 0 ]; then STAGE=early; else STAGE=late; fi
  echo "--- P$i (center C$((i%3+1)), stage $STAGE) ---"
  curl -s -X POST "$BASE/v1/studies/DEMO-1/allocations" \
    -H "$INV" -H 'Content-Type: application/json' \
    -H "Idempotency-Key: demo-key-$i" \
    -H "X-Request-ID: demo-req-$i" \
    -d "{\"subject_id\":\"P$i\",\"features\":{\"center\":\"C$((i%3+1))\",\"stage\":\"$STAGE\"}}" \
    | python3 -c "import sys,json;d=json.load(sys.stdin);x=d['data'];print(x['outcome'],x['subject_id'],x['stratum_key'],'blk',x['block_index'],'pos',x['position'],'->',x['arm'])"
done

echo '== 4) 重复请求返回原分配（幂等，不重新随机）=='
curl -s -X POST "$BASE/v1/studies/DEMO-1/allocations" -H "$INV" \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: demo-key-0' \
  -d '{"subject_id":"P0","features":{"center":"C1","stage":"early"}}' | python3 -m json.tool

echo '== 5) 特征被改 → 409 稳定类别（不会偷偷重新随机）=='
curl -s -X POST "$BASE/v1/studies/DEMO-1/allocations" -H "$INV" \
  -H 'Content-Type: application/json' \
  -d '{"subject_id":"P0","features":{"center":"C2","stage":"early"}}' | python3 -m json.tool

echo '== 6) 审计员：均衡表（尾组单列不确定）=='
curl -s "$BASE/v1/studies/DEMO-1/evidence/balance" -H "$AUD" | python3 -m json.tool

echo '== 7) 审计员：随机流逐数复核 =='
curl -s -X POST "$BASE/v1/studies/DEMO-1/evidence/verify" -H "$AUD" | python3 -m json.tool

echo '== 8) 审计员：审计事件 =='
curl -s "$BASE/v1/studies/DEMO-1/audit?limit=5" -H "$AUD" | python3 -m json.tool

echo '== 9) 分布诊断（固定种子扫描；不显著≠证明）=='
curl -s -X POST "$BASE/v1/diagnostics/distribution" -H "$AUD" \
  -H 'Content-Type: application/json' \
  -d '{"block_size":4,"arms":[{"arm_id":"control","ratio":1},{"arm_id":"treatment","ratio":1}],"blocks_per_seed":200}' \
  | python3 -m json.tool

echo '== 10) 权限隔离：auditor 登记应 403 =='
curl -s -o /dev/null -w '%{http_code}\n' -X POST \
  "$BASE/v1/studies/DEMO-1/allocations" -H "$AUD" \
  -H 'Content-Type: application/json' \
  -d '{"subject_id":"X","features":{"center":"C1","stage":"early"}}'
