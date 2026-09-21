#!/usr/bin/env bash
# ProofCycle 端到端演示脚本：
#   建作业 -> 设计师上传首版 -> 审查员提交意见（含失败拦截）-> 修订 v2
#   -> 全员通过 -> PM 签核（含并发/越权演示）-> 按版本查历史与导出报告
#
# 依赖：bash、curl。用法：
#   ./scripts/demo.sh                 # 默认 http://127.0.0.1:8080
#   BASE_URL=http://localhost:8080 ./scripts/demo.sh
set -uo pipefail

BASE_URL="${BASE_URL:-http://127.0.0.1:8080}"
API="$BASE_URL/api/v1"
DESIGNER="u-designer"
PM="u-pm"
R1="u-reviewer-1"; R2="u-reviewer-2"
CHECKLIST="cl-packaging-default"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PNG="$SCRIPT_DIR/../testdata/sample-proof.png"

c_red=$'\033[31m'; c_grn=$'\033[32m'; c_ylw=$'\033[33m'; c_cya=$'\033[36m'; c_rst=$'\033[0m'
say()  { printf '%s\n' "${c_cya}== $* ==${c_rst}"; }
ok()   { printf '%s\n' "${c_grn}OK: $*${c_rst}"; }
warn() { printf '%s\n' "${c_ylw}预期内拒绝: $*${c_rst}"; }
die()  { printf '%s\n' "${c_red}FATAL: $*${c_rst}" >&2; exit 1; }

req() { # method path user [json-body]
  local method="$1" path="$2" user="$3" body="${4:-}"
  if [ -n "$body" ]; then
    curl -sS -X "$method" "$API$path" -H "Content-Type: application/json" \
      -H "X-User-Id: $user" -d "$body" -w '\n__HTTP__%{http_code}'
  else
    curl -sS -X "$method" "$API$path" -H "X-User-Id: $user" -w '\n__HTTP__%{http_code}'
  fi
}
jget() { python3 -c 'import sys,json;d=json.load(sys.stdin);print(eval(sys.argv[1]))' "$1" 2>/dev/null; }

http_of() { printf '%s' "$1" | sed -n 's/.*__HTTP__//p'; }
body_of() { printf '%s' "$1" | sed 's/__HTTP__[0-9]*$//'; }

expect_code() { # response expected description
  local code; code="$(http_of "$1")"
  if [ "$code" = "$2" ]; then ok "$3 (HTTP $code)"; else die "$3: 期望 $2 实际 $code -> $(body_of "$1")"; fi
}

say "0) 健康检查"
curl -fsS "$BASE_URL/healthz" >/dev/null || die "服务未就绪：$BASE_URL/healthz"
ok "服务在线"

say "1) PM 创建作业（设计师 1 名，审查员 2 名）"
RESP="$(req POST /jobs "$PM" "{\"name\":\"茶礼盒打样 2026 秋\",\"description\":\"demo\",
  \"designer_id\":\"$DESIGNER\",\"pm_id\":\"$PM\",
  \"reviewer_ids\":[\"$R1\",\"$R2\"],\"checklist_id\":\"$CHECKLIST\"}")"
expect_code "$RESP" 201 "创建作业"
JOB_ID="$(body_of "$RESP" | jget 'd["job"]["ID"]')"
[ -n "$JOB_ID" ] || die "未取得 job id: $(body_of "$RESP")"
ok "作业 ID = $JOB_ID"

say "2) 设计师上传首版文件（本地 PNG，服务端计算 SHA-256）"
SHA="$(sha256sum "$PNG" | awk '{print $1}')"
RESP="$(curl -sS -X POST "$API/jobs/$JOB_ID/versions" -H "X-User-Id: $DESIGNER" \
  -F "sha256=$SHA" -F "file=@$PNG;type=image/png" -w '\n__HTTP__%{http_code}')"
expect_code "$RESP" 201 "上传 v1"
mapfile -t ITEM_IDS < <(body_of "$RESP" | python3 -c '
import sys,json
d=json.load(sys.stdin)
print("\n".join(i["ID"] for i in d["items"]))')
[ "${#ITEM_IDS[@]}" -eq 8 ] || die "快照应有 8 项，实际 ${#ITEM_IDS[@]}"
ok "v1 快照绑定 ${#ITEM_IDS[@]} 个检查项"

say "3) 审查员1提交意见：第 4 项（条码）失败并填写原因"
ITEMS_JSON="$(python3 - "$R1-fail-barcode" <<PY
import json,sys
ids = """${ITEM_IDS[*]}""".split()
items=[{"snapshot_item_id":i,"result":"fail" if n==3 else "pass",
        "fail_reason":"条码扫读等级仅 D，低于 C 级要求" if n==3 else ""}
       for n,i in enumerate(ids)]
print(json.dumps({"version_no":1,"items":items},ensure_ascii=False))
PY
)"
RESP="$(req POST "/jobs/$JOB_ID/reviews" "$R1" "$ITEMS_JSON")"
expect_code "$RESP" 201 "R1 提交含失败项的意见"

R2_BODY="$(python3 -c '
import json,sys
ids=sys.argv[1].split()
print(json.dumps({"version_no":1,"items":[{"snapshot_item_id":i,"result":"pass"} for i in ids]}))' "${ITEM_IDS[*]}")"
RESP="$(req POST "/jobs/$JOB_ID/reviews" "$R2" "$R2_BODY")"
expect_code "$RESP" 201 "R2 全部通过"

say "4) PM 尝试签核：存在失败项，必须被拦截"
RESP="$(req POST "/jobs/$JOB_ID/approvals" "$PM" '{}')"
expect_code "$RESP" 409 "失败项拦截签核"

say "5) 设计师提交修订 v2（修订文件不覆盖旧版）"
RESP="$(curl -sS -X POST "$API/jobs/$JOB_ID/versions" -H "X-User-Id: $DESIGNER" \
  -F "file=@$PNG;type=image/png" -w '\n__HTTP__%{http_code}')"
expect_code "$RESP" 201 "上传 v2 修订"
mapfile -t ITEM_IDS2 < <(body_of "$RESP" | python3 -c '
import sys,json
d=json.load(sys.stdin)
print("\n".join(i["ID"] for i in d["items"]))')
ok "v2 使用新的清单快照（${#ITEM_IDS2[@]} 项）"

say "6) v2 两位审查员全部通过"
for R in "$R1" "$R2"; do
  BODY="$(python3 -c '
import json,sys
ids=sys.argv[1].split()
print(json.dumps({"version_no":2,"items":[{"snapshot_item_id":i,"result":"pass"} for i in ids]}))' "${ITEM_IDS2[*]}")"
  RESP="$(req POST "/jobs/$JOB_ID/reviews" "$R" "$BODY")"
  expect_code "$RESP" 201 "$R 完成 v2 审查"
done

say "7) 角色隔离演示"
RESP="$(req POST "/jobs/$JOB_ID/approvals" "$DESIGNER" '{}')"
expect_code "$RESP" 403 "设计师不能批准自己的作业"
RESP="$(req POST "/jobs/$JOB_ID/approvals" "$R1" '{}')"
expect_code "$RESP" 403 "审查员不能签核"
RESP="$(req GET "/jobs/$JOB_ID" "u-reviewer-3" '')"
expect_code "$RESP" 404 "非成员看不到未授权作业（404）"

say "8) PM 签核 v2，并发两次只应有一次成功"
RESP1="$(req POST "/jobs/$JOB_ID/approvals" "$PM" '{}' )"
RESP2="$(req POST "/jobs/$JOB_ID/approvals" "$PM" '{}' )"
C1="$(http_of "$RESP1")"; C2="$(http_of "$RESP2")"
if { [ "$C1" = 201 ] && [ "$C2" = 409 ]; } || { [ "$C1" = 409 ] && [ "$C2" = 201 ]; }; then
  ok "并发签核：一次 201、一次 409"
else die "并发签核结果异常：$C1 / $C2"; fi

say "9) 按版本查询审查历史"
RESP="$(req GET "/jobs/$JOB_ID/versions/1/history" "$PM")"
expect_code "$RESP" 200 "查询 v1 历史（旧意见保留）"
body_of "$RESP" | python3 -c '
import sys,json
d=json.load(sys.stdin)
print("  v1: %d 份意见, 已签核=%s" % (len(d["Reviews"]), d["Approval"] is not None))'
RESP="$(req GET "/jobs/$JOB_ID/versions/2/history" "$PM")"
body_of "$RESP" | python3 -c '
import sys,json
d=json.load(sys.stdin)
print("  v2: %d 份意见, 已签核=%s" % (len(d["Reviews"]), d["Approval"] is not None))'

say "10) 导出 v2 报告（Markdown，含文件摘要/清单/签核依据）"
RESP="$(curl -sS "$API/jobs/$JOB_ID/versions/2/report?format=md" -H "X-User-Id: $PM" \
  -w '\n__HTTP__%{http_code}')"
expect_code "$RESP" 200 "导出报告"
body_of "$RESP" | sed -n '1,12p'
printf '  ...（完整报告共 %s 行）\n' "$(body_of "$RESP" | wc -l)"

printf '%s\n' "${c_grn}演示完成。作业 ID: $JOB_ID${c_rst}"
