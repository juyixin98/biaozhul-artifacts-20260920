#!/usr/bin/env bash
# ProofCycle 演示脚本：走通 建作业 → 上传修订 → 审查 → 签核 → 导出报告 全流程。
# 依赖：curl、jq。前置：服务已启动且 SEED_DEMO=true（docker compose 默认开启）。
set -euo pipefail

BASE=${BASE:-http://localhost:8080}
ALICE=(-H "X-User-ID: 1")   # 项目经理
BOB=(-H "X-User-ID: 2")     # 设计师
CAROL=(-H "X-User-ID: 3")   # 审查员
DAVE=(-H "X-User-ID: 4")    # 审查员

echo "== 1. 项目经理创建作业（设计师 bob，审查员 carol/dave） =="
JOB=$(curl -sf "${ALICE[@]}" -H 'Content-Type: application/json' -d '{
  "title": "彩盒打样审查",
  "designer_id": 2,
  "reviewer_ids": [3, 4],
  "checklist": ["专色与色票一致", "条码可扫描", "裁切线位置正确"]
}' "$BASE/jobs")
JOB_ID=$(echo "$JOB" | jq -r .id)
echo "job_id=$JOB_ID"

echo "== 2. 设计师上传第一版（失败项拦截演示） =="
printf '%%PDF-1.4\nproof v1 demo content' > /tmp/proof-v1.pdf
REV1=$(curl -sf "${BOB[@]}" -F "file=@/tmp/proof-v1.pdf;filename=proof.pdf" "$BASE/jobs/$JOB_ID/revisions")
REV1_ID=$(echo "$REV1" | jq -r .id)
echo "revision v1 id=$REV1_ID sha256=$(echo "$REV1" | jq -r .sha256)"

ITEMS=$(curl -sf "${CAROL[@]}" "$BASE/jobs/$JOB_ID/revisions/$REV1_ID/history" | jq -c '.checklist')
ITEM1=$(echo "$ITEMS" | jq -r '.[0].id')
ITEM2=$(echo "$ITEMS" | jq -r '.[1].id')
ITEM3=$(echo "$ITEMS" | jq -r '.[2].id')

echo "-- carol 标记检查项1失败（带原因），其余通过 --"
curl -sf "${CAROL[@]}" -X PUT -H 'Content-Type: application/json' \
  -d '{"status":"fail","fail_reason":"Pantone 186C 偏暗","expected_version":1}' \
  "$BASE/jobs/$JOB_ID/revisions/$REV1_ID/checklist/$ITEM1" > /dev/null
curl -sf "${CAROL[@]}" -X PUT -H 'Content-Type: application/json' \
  -d '{"status":"pass","expected_version":1}' \
  "$BASE/jobs/$JOB_ID/revisions/$REV1_ID/checklist/$ITEM2" > /dev/null
curl -sf "${DAVE[@]}" -X PUT -H 'Content-Type: application/json' \
  -d '{"status":"pass","expected_version":1}' \
  "$BASE/jobs/$JOB_ID/revisions/$REV1_ID/checklist/$ITEM3" > /dev/null
curl -sf "${CAROL[@]}" -X POST -H 'Content-Type: application/json' \
  -d '{"content":"专色需要重调"}' "$BASE/jobs/$JOB_ID/revisions/$REV1_ID/comments" > /dev/null
curl -sf "${CAROL[@]}" -X POST "$BASE/jobs/$JOB_ID/revisions/$REV1_ID/complete" > /dev/null
curl -sf "${DAVE[@]}" -X POST "$BASE/jobs/$JOB_ID/revisions/$REV1_ID/complete" > /dev/null

echo "-- 存在失败项，签核应被拒绝 --"
curl -s "${ALICE[@]}" -X POST "$BASE/jobs/$JOB_ID/approve" | jq .

echo "== 3. 设计师提交第二版（旧版本意见保留但失效） =="
printf '%%PDF-1.4\nproof v2 fixed color' > /tmp/proof-v2.pdf
REV2=$(curl -sf "${BOB[@]}" -F "file=@/tmp/proof-v2.pdf;filename=proof.pdf" "$BASE/jobs/$JOB_ID/revisions")
REV2_ID=$(echo "$REV2" | jq -r .id)
echo "revision v2 id=$REV2_ID"

ITEMS=$(curl -sf "${CAROL[@]}" "$BASE/jobs/$JOB_ID/revisions/$REV2_ID/history" | jq -c '.checklist')
i=0
for ITEM_ID in $(echo "$ITEMS" | jq -r '.[].id'); do
  i=$((i+1))
  curl -sf "${CAROL[@]}" -X PUT -H 'Content-Type: application/json' \
    -d '{"status":"pass","expected_version":1}' \
    "$BASE/jobs/$JOB_ID/revisions/$REV2_ID/checklist/$ITEM_ID" > /dev/null
done
curl -sf "${CAROL[@]}" -X POST "$BASE/jobs/$JOB_ID/revisions/$REV2_ID/complete" > /dev/null
curl -sf "${DAVE[@]}" -X POST "$BASE/jobs/$JOB_ID/revisions/$REV2_ID/complete" > /dev/null

echo "== 4. 全部审查员完成且无失败项，项目经理签核 =="
curl -sf "${ALICE[@]}" -X POST "$BASE/jobs/$JOB_ID/approve" | jq .

echo "== 5. 导出报告（含文件摘要、清单快照与签核依据） =="
curl -sf "${ALICE[@]}" "$BASE/jobs/$JOB_ID/report" | jq .

echo "== 6. 按版本回看历史（v1 意见仍在，但已失效） =="
curl -sf "${CAROL[@]}" "$BASE/jobs/$JOB_ID/revisions/$REV1_ID/history" | jq '{revision: .revision.status, comments: .comments}'

echo "演示完成。"
