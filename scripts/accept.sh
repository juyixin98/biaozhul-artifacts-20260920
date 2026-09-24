#!/usr/bin/env bash
# ============================================================================
# 一键验收：产物晋级原子性（Go + chi + PostgreSQL）
#
# 覆盖：
#   1) 不可变引用（digest/证据版本/策略版本），浮动标签被拒绝
#   2) 完整 happy path + 服务端签名收据的离线验真
#   3) 复制失败（真实字节损坏 → 复制后 sha256 校验不过 → 旧指针不变）
#   4) 指针提交前崩溃（进程 os.Exit → 重启恢复 → 旧版本保持可用 → 重试成功）
#   5) 并发晋级（expected_gen CAS，一胜一 conflict）
#   6) 迟到审批（审批钉旧代次，被拒；审批不被消费）
#   7) 并发回退 / 回退只能指向完整且仍满足保留规则的历史产物
#   8) 幂等键；每次尝试（含失败）完整落库
#
# 用法: scripts/accept.sh            # 自动建库/起服务/生成示例/跑全部场景
#       KEEP_SERVER=1 scripts/accept.sh
# ============================================================================
set -euo pipefail
cd "$(dirname "$0")/.."

PORT=${PORT:-18080}
BASE=${BASE_URL:-http://127.0.0.1:$PORT}
EX=examples
WORK=.accept-work
BLOB=.accept-blobs
SIGNING=.accept-keys
BIN="$WORK/bin"
export PGDATABASE=${PGDATABASE:-promo_atomic}
if [[ -z "${DATABASE_URL:-}" ]]; then
  export DATABASE_URL="postgresql://promo:promo_dev_pwd@127.0.0.1:5432/$PGDATABASE?sslmode=disable"
fi

PASS=0; FAIL=0
RED=$'\033[31m'; GREEN=$'\033[32m'; YEL=$'\033[33m'; NC=$'\033[0m'

ok()   { printf '%s PASS%s %s\n' "$GREEN" "$NC" "$*"; PASS=$((PASS+1)); }
bad()  { printf '%s FAIL%s %s\n' "$RED" "$NC" "$*"; FAIL=$((FAIL+1)); }
info() { printf '%s---- %s%s\n' "$YEL" "$*" "$NC"; }
die()  { bad "$*"; exit 1; }

# api METHOD PATH [JSON]  -> body in $BODY, http code in $CODE
api() {
  local method=$1 path=$2 data=${3:-}
  if [[ -n "$data" ]]; then
    CODE=$(curl -sS -o /tmp/accept-body -w '%{http_code}' -X "$method" "$BASE$path" \
      -H 'Content-Type: application/json' --data "$data")
  else
    CODE=$(curl -sS -o /tmp/accept-body -w '%{http_code}' -X "$method" "$BASE$path")
  fi
  BODY=$(cat /tmp/accept-body)
}

# expect_field <jq filter> <expected> <description>
expect_field() {
  local filter=$1 want=$2 desc=$3
  local got
  got=$(jq -c "$filter" <<<"$BODY" 2>/dev/null)
  if [[ "$got" == "$want" ]]; then ok "$desc ($got)"; else bad "$desc: want $want got $got; body=$BODY"; fi
}
expect_code() { [[ "$CODE" == "$1" ]] && ok "HTTP $1 — $2" || bad "$2: want HTTP $1 got $CODE body=$BODY"; }

# parallel_post <outfile> <json>：独立临时文件，避免并发请求共享 /tmp/accept-body
parallel_post() {
  local out=$1 data=$2 tmp
  tmp=$(mktemp "$WORK/post.XXXXXX")
  curl -sS -o "$tmp" -X POST "$BASE/v1/promotions" \
    -H 'Content-Type: application/json' --data "$data" >/dev/null
  cp "$tmp" "$out"
  rm -f "$tmp"
}
parallel_post_rb() {
  local out=$1 data=$2 tmp
  tmp=$(mktemp "$WORK/post.XXXXXX")
  curl -sS -o "$tmp" -X POST "$BASE/v1/rollbacks" \
    -H 'Content-Type: application/json' --data "$data" >/dev/null
  cp "$tmp" "$out"
  rm -f "$tmp"
}

sign_approval() { # id env digest evid evver gen > json
  "$BIN/signapproval" -key "$EX/keys/approver.key" \
    -id "$1" -env "$2" -digest "$3" -evidence "$4" -evidence-version "$5" -expected-gen "$6"
}

trap ' [[ -n "${SRV_PID:-}" ]] && kill "$SRV_PID" 2>/dev/null || true' EXIT

# ---------- 0. 准备 ----------
info "准备数据库/示例/服务"
bash scripts/db-prepare.sh
mkdir -p "$WORK" "$BIN"
go build -o "$BIN/server"   ./cmd/server   || die "编译 server 失败"
go build -o "$BIN/signapproval" ./cmd/signapproval || die "编译 signapproval 失败"
go build -o "$BIN/verifyreceipt" ./cmd/verifyreceipt || die "编译 verifyreceipt 失败"
go build -o "$BIN/genexample" ./cmd/genexample || die "编译 genexample 失败"
"$BIN/genexample" -out "$EX" >/dev/null

# 重置业务库，保证验收可重复运行（必须在服务启动并完成 migration 之后）
reset_db() {
  psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -q <<'SQL'
TRUNCATE gc_runs, env_history, env_blobs, env_pointers,
         promotion_attempts, approvals, test_evidence, policies, tags, artifacts
RESTART IDENTITY CASCADE;
SQL
}

ALLOW_FAULT_INJECTION=1 BLOB_STORE_DIR="$PWD/$BLOB" SIGNING_KEY_DIR="$PWD/$SIGNING" \
  HTTP_ADDR=":$PORT" MIGRATIONS_DIR="$PWD/migrations" "$BIN/server" >"$WORK/server.log" 2>&1 &
SRV_PID=$!
for _ in $(seq 1 50); do curl -sf "$BASE/healthz" >/dev/null && break; sleep 0.2; done
curl -sf "$BASE/healthz" >/dev/null || { cat "$WORK/server.log"; die "服务未就绪"; }
reset_db

DA=$(jq -r .artifact_digest "$EX/evidence-a-v2.json")
DB_=$(jq -r .artifact_digest "$EX/evidence-b-v1.json")
DC=$(jq -r .artifact_digest "$EX/evidence-c-v1.json")
APPROVER=$(cat "$EX/keys/approver.pub")

# ---------- 1. 登记不可变对象 ----------
info "上传 3 个产物（digest 服务端实算）"
for n in a b c; do
  RESP=$(curl -sS -X POST "$BASE/v1/artifacts" -F "content=@$EX/artifacts/$n.bin")
  echo "$RESP" > "$WORK/upload-$n.json"
done
jq -e ".digest==\"$DA\"" "$WORK/upload-a.json" >/dev/null && ok "A 实算 digest 一致 $DA" || bad "A digest"
# 声明错误 digest 必须被拒
CODE=$(curl -sS -o "$WORK/upbad.json" -w '%{http_code}' -X POST "$BASE/v1/artifacts" \
  -F "content=@$EX/artifacts/a.bin" -F "digest=sha256:$(printf x%.0s {1..64})")
[[ "$CODE" == 400 ]] && ok "错误声明 digest 被拒 400" || bad "错误声明 digest: $CODE $(cat "$WORK/upbad.json")"
# 本地 sha256sum 与服务端结果交叉核对
LOCAL=$(sha256sum "$EX/artifacts/a.bin" | cut -d' ' -f1)
[[ "$LOCAL" == "${DA#sha256:}" ]] && ok "服务端 sha256 与 sha256sum 一致" || bad "sha256 交叉核对"

info "登记证据与策略（真实 Ed25519 验签）"
api POST /v1/evidence "$(cat "$EX/evidence-a-v1.json")"; expect_code 201 "证据 A v1"
api POST /v1/evidence "$(cat "$EX/evidence-a-v2.json")"; expect_code 201 "证据 A v2（不可变新版本）"
api POST /v1/evidence "$(cat "$EX/evidence-b-v1.json")"; expect_code 201 "证据 B v1"
api POST /v1/evidence "$(cat "$EX/evidence-c-v1.json")"; expect_code 201 "证据 C v1"
api POST /v1/policies "$(cat "$EX/policy-v1.json")"; expect_code 201 "策略 v1"
api POST /v1/policies "$(cat "$EX/policy-v2.json")"; expect_code 201 "策略 v2（要求安全测试+审批）"
api POST /v1/policies "$(cat "$EX/policy-v3.json")"; expect_code 201 "策略 v3（同准入、保留规则更宽松 keep_last_n=4）"

# 篡改证据签名 → 拒绝
BAD=$(jq '.signature="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="' "$EX/evidence-b-v1.json")
api POST /v1/evidence "$BAD"
{ [[ "$CODE" == 422 ]] && jq -e '.error|test("signature")' >/dev/null <<<"$BODY"; } \
  && ok "伪造证据签名被拒" || bad "伪造证据: $CODE $BODY"

# v1 策略不要求 security_scan：A 的证据 v1 满足；v2 不满足（security_scan=false）
# 浮动标签：先存在，但晋升不能引用
api PUT /v1/tags/candidate "{\"digest\":\"$DA\"}"; expect_code 200 "浮动标签 candidate->A"

# ---------- 2. 拒绝浮动引用 ----------
info "浮动标签/伪 digest 引用必须被拒"
api POST /v1/promotions '{"env":"staging","digest":"candidate","evidence_id":"ev-a","evidence_version":2,"policy_id":"promotion-policy","policy_version":1,"expected_gen":0}'
{ [[ "$CODE" == 200 ]] && jq -e '.status=="rejected" and (.reason|test("immutable|sha256"))' >/dev/null <<<"$BODY"; } \
  && ok "按标签晋升被拒（rejected，尝试留证）" || bad "标签晋升: $CODE $BODY"

# v2 策略 + A 的证据 v1（缺安全测试）→ 拒绝
api POST /v1/promotions '{"env":"staging","digest":"'"$DA"'","evidence_id":"ev-a","evidence_version":1,"policy_id":"promotion-policy","policy_version":2,"expected_gen":0}'
expect_field .status '"rejected"' "策略 v2 拒绝未过安全测试的证据 v1"

# ---------- 3. 首次晋级 A（带审批，gen0→1） ----------
info "happy path: A staging gen0 -> gen1"
api POST /v1/approvals "$(cat "$EX/approval-a-staging-gen0.json")"; expect_code 201 "登记 A 的 gen0 审批"
PROMO_A=$(jq -nc --arg d "$DA" --arg a "$(jq -c . "$EX/approval-a-staging-gen0.json" | jq -r .approval_id)" \
  '{env:"staging",digest:$d,evidence_id:"ev-a",evidence_version:2,policy_id:"promotion-policy",policy_version:2,expected_gen:0,approval_id:$a}')
api POST /v1/promotions "$PROMO_A"
expect_code 201 "A 晋级返回 201"
expect_field .status '"committed"' "A 已提交"
expect_field .gen 1 "gen=1"
jq -c .receipt > "$WORK/receipt-a.json" <<<"$BODY"
"$BIN/verifyreceipt" -f "$WORK/receipt-a.json" >/dev/null && ok "收据 A 离线 Ed25519 验真通过" || bad "收据 A 验签"
api GET /v1/envs/staging
expect_field .gen 1 "环境读模型 gen=1"; expect_field .current_digest "\"$DA\"" "指针指向 A"

# 幂等：同 idem key 重放 → 同一 attempt
IDEM=$(jq -nc --arg d "$DB_" '{env:"staging",digest:$d,evidence_id:"ev-b",evidence_version:1,policy_id:"promotion-policy",policy_version:2,expected_gen:1,idempotency_key:"idem-b-1"}')
APR_B1=$(sign_approval apr-b-gen1 staging "$DB_" ev-b 1 1); api POST /v1/approvals "$APR_B1"; expect_code 201 "审批 B gen1"
IDEM=$(jq --arg a "$(jq -r .approval_id <<<"$APR_B1")" '.approval_id=$a' <<<"$IDEM")
api POST /v1/promotions "$IDEM"; AID1=$(jq -r .attempt_id <<<"$BODY")
api POST /v1/promotions "$IDEM"; AID2=$(jq -r .attempt_id <<<"$BODY")
[[ "$AID1" == "$AID2" && -n "$AID1" ]] && ok "幂等键重放返回同一尝试 $AID1" || bad "幂等: $AID1 != $AID2"
api GET /v1/envs/staging; expect_field .gen 2 "B 晋级后 gen=2"

# 审批复用：同一 approval 再用一次 → rejected
REUSE=$(jq -nc --arg d "$DC" '{env:"staging",digest:$d,evidence_id:"ev-c",evidence_version:1,policy_id:"promotion-policy",policy_version:2,expected_gen:2,approval_id:"apr-b-gen1"}')
api POST /v1/promotions "$REUSE"
expect_field .status '"rejected"' "审批签名不可二次消费"

# ---------- 4. 并发晋级（同 expected_gen，一胜一 conflict） ----------
info "并发晋级：两个同 gen=0 请求，CAS 决定唯一赢家"
APR_RB=$(sign_approval apr-race-b race "$DB_" ev-b 1 0)
APR_RC=$(sign_approval apr-race-c race "$DC" ev-c 1 0)
api POST /v1/approvals "$APR_RB"; api POST /v1/approvals "$APR_RC"
RB=$(jq -nc --arg d "$DB_" '{env:"race",digest:$d,evidence_id:"ev-b",evidence_version:1,policy_id:"promotion-policy",policy_version:2,expected_gen:0,approval_id:"apr-race-b"}')
RC=$(jq -nc --arg d "$DC" '{env:"race",digest:$d,evidence_id:"ev-c",evidence_version:1,policy_id:"promotion-policy",policy_version:2,expected_gen:0,approval_id:"apr-race-c"}')
parallel_post "$WORK/r1.json" "$RB" &
P1=$!
parallel_post "$WORK/r2.json" "$RC" &
P2=$!
wait "$P1" "$P2"
S1=$(jq -r .status "$WORK/r1.json"); S2=$(jq -r .status "$WORK/r2.json")
WIN=$(printf '%s\n%s\n' "$S1" "$S2" | grep -c committed)
CONF=$(printf '%s\n%s\n' "$S1" "$S2" | grep -c conflict)
[[ "$WIN" == 1 && "$CONF" == 1 ]] && ok "并发：恰好 1 committed / 1 conflict ($S1,$S2)" || bad "并发结果: $S1 $S2"
api GET /v1/envs/race; G=$(jq -r .gen <<<"$BODY"); [[ "$G" == 1 ]] && ok "race 环境只前进一代 gen=1" || bad "race gen=$G"

# ---------- 5. 迟到审批 ----------
info "迟到审批：审批钉的代次已被别的晋升越过 → 拒绝且审批保留 valid"
LATE=$(sign_approval apr-late staging "$DC" ev-c 1 1) # 钉 gen1，但 staging 已在 gen2
api POST /v1/approvals "$LATE"; expect_code 201 "迟到审批已登记（保留为证据）"
api POST /v1/promotions/approve '{"approval_id":"apr-late"}'
expect_field .status '"conflict"' "迟到审批晋级被 conflict 拒绝"
api GET /v1/envs/staging; expect_field .gen 2 "迟到审批未推动代次"
psql "$DATABASE_URL" -tAc "SELECT status FROM approvals WHERE approval_id='apr-late'" | grep -q valid \
  && ok "迟到审批未被消费（valid 留档）" || bad "迟到审批状态"

# ---------- 6. 复制失败：真实字节损坏 ----------
info "复制失败：故障头翻转复制字节 → 重新 sha256 校验不过 → copy_failed，旧指针不变"
api GET /v1/envs/staging; GEN_BEFORE=$(jq -r .gen <<<"$BODY"); PTR_BEFORE=$(jq -r .current_digest <<<"$BODY")
APR_C2=$(sign_approval apr-c-gen2 staging "$DC" ev-c 1 2)
api POST /v1/approvals "$APR_C2"; expect_code 201 "审批 C gen2"
CODE=$(curl -sS -o "$WORK/corrupt.json" -w '%{http_code}' -X POST "$BASE/v1/promotions" \
  -H 'Content-Type: application/json' -H 'X-Test-Fault: corrupt-copy' \
  --data "$(jq -nc --arg d "$DC" '{env:"staging",digest:$d,evidence_id:"ev-c",evidence_version:1,policy_id:"promotion-policy",policy_version:2,expected_gen:2,approval_id:"apr-c-gen2"}')")
jq -e '.status=="copy_failed" and (.reason|test("verification"))' "$WORK/corrupt.json" >/dev/null \
  && ok "复制损坏被校验拦截（copy_failed）" || bad "复制损坏: $(cat "$WORK/corrupt.json")"
# 该次尝试必须完整留证
CID=$(jq -r .attempt_id "$WORK/corrupt.json")
api GET /v1/attempts/$CID
jq -e '.status=="copy_failed" and .failure_stage=="copy" and (.copy_dst_path|type=="string") and .finished_at!=null' >/dev/null <<<"$BODY" \
  && ok "失败尝试完整证据已落库（含 src/dst/stage/reason）" || bad "失败尝试证据: $BODY"
api GET /v1/envs/staging
[[ "$(jq -r .gen <<<"$BODY")" == "$GEN_BEFORE" && "$(jq -r .current_digest <<<"$BODY")" == "$PTR_BEFORE" ]] \
  && ok "复制失败后旧指针原样可用 gen=$GEN_BEFORE" || bad "旧指针被破坏"
# 损坏尝试是否消费了审批？（复制发生在提交事务之前，不应消费）
psql "$DATABASE_URL" -tAc "SELECT status FROM approvals WHERE approval_id='apr-c-gen2'" | grep -q valid \
  && ok "复制失败未消费审批（可随重试复用）" || bad "复制失败误消费审批"

# 不带故障的正常重试（同 expected_gen=2，同审批）→ 成功
api POST /v1/promotions "$(jq -nc --arg d "$DC" '{env:"staging",digest:$d,evidence_id:"ev-c",evidence_version:1,policy_id:"promotion-policy",policy_version:2,expected_gen:2,approval_id:"apr-c-gen2"}')"
expect_field .status '"committed"' "无故障重试 C 成功（复制幂等复用）"
api GET /v1/envs/staging; expect_field .gen 3 "staging gen=3"

# ---------- 7. 指针提交前崩溃 ----------
info "提交前崩溃：A 晋级 crash-env，故障使进程在副本校验后、提交前 os.Exit"
APR_K0=$(sign_approval apr-crash-a crash "$DA" ev-a 2 0)
api POST /v1/approvals "$APR_K0"; expect_code 201 "crash env 审批 gen0"
HTTP=$(curl -sS -o "$WORK/crash1.json" -w '%{http_code}' -X POST "$BASE/v1/promotions" \
  -H 'Content-Type: application/json' -H 'X-Test-Fault: crash-before-commit' \
  --data "$(jq -nc --arg d "$DA" '{env:"crash",digest:$d,evidence_id:"ev-a",evidence_version:2,policy_id:"promotion-policy",policy_version:2,expected_gen:0,approval_id:"apr-crash-a"}')" \
  --max-time 5) || HTTP=000
[[ "$HTTP" == "000" ]] && ok "连接随进程崩溃断开" || bad "崩溃请求返回 $HTTP"
for _ in $(seq 1 30); do kill -0 "$SRV_PID" 2>/dev/null || break; sleep 0.2; done
kill -0 "$SRV_PID" 2>/dev/null && bad "服务器没有退出" || ok "服务器已退出(99)"

# 副本已在磁盘（校验通过），指针未提交
restart_server() {
  ALLOW_FAULT_INJECTION=1 BLOB_STORE_DIR="$PWD/$BLOB" SIGNING_KEY_DIR="$PWD/$SIGNING" \
    HTTP_ADDR=":$PORT" MIGRATIONS_DIR="$PWD/migrations" "$BIN/server" >>"$WORK/server.log" 2>&1 &
  SRV_PID=$!
  for _ in $(seq 1 50); do curl -sf "$BASE/healthz" >/dev/null && break; sleep 0.2; done
  curl -sf "$BASE/healthz" >/dev/null || die "重启失败"
}
restart_server
grep -q "recovered_aborted" "$WORK/server.log" && ok "重启恢复把崩溃尝试标记为 recovered_aborted" || bad "未见恢复日志"
api GET /v1/envs/crash
expect_field .gen 0 "崩溃后指针仍是空（gen0），旧可用版本不受影响"
psql "$DATABASE_URL" -tAc "SELECT count(*) FROM promotion_attempts WHERE env='crash' AND status='recovered_aborted'" | grep -q 1 \
  && ok "崩溃尝试留证 recovered_aborted" || bad "崩溃尝试状态"

# 用相同 expected_gen=0 重试 → 已校验副本幂等复用，提交成功
api POST /v1/promotions "$(jq -nc --arg d "$DA" '{env:"crash",digest:$d,evidence_id:"ev-a",evidence_version:2,policy_id:"promotion-policy",policy_version:2,expected_gen:0,approval_id:"apr-crash-a"}')"
expect_field .status '"committed"' "崩溃后重试成功"
api GET /v1/envs/crash; expect_field .gen 1 "crash gen=1"

# ---------- 8. 回退：并发回退 + 保留规则 ----------
info "回退：只能指向完整且仍满足保留规则的历史产物（用 v3 保留规则重检，keep_last_n=4）"
# staging 历史: 1:A 2:B 3:C；回退到 gen1(A)
api POST /v1/rollbacks '{"env":"staging","target_gen":1,"expected_gen":3,"policy_id":"promotion-policy","policy_version":3}'
expect_field .status '"committed"' "回退到历史 gen1 的 A（完整且在保留集）"
api GET /v1/envs/staging; expect_field .gen 4 "回退也产生新代次 gen=4"
expect_field .current_digest "\"$DA\"" "指针回到 A"

# 回退到从未进入历史的 digest → 409
api POST /v1/rollbacks '{"env":"staging","digest":"sha256:0000000000000000000000000000000000000000000000000000000000000000","expected_gen":4,"policy_id":"promotion-policy","policy_version":3}'
expect_code 409 "回退未知 digest 被拒"

# 并发回退：两个请求都基于 gen4，目标 gen2(B)/gen3(C) → 一胜一 conflict
parallel_post_rb "$WORK/rb1.json" '{"env":"staging","target_gen":2,"expected_gen":4,"policy_id":"promotion-policy","policy_version":3}' &
Q1=$!
parallel_post_rb "$WORK/rb2.json" '{"env":"staging","target_gen":3,"expected_gen":4,"policy_id":"promotion-policy","policy_version":3}' &
Q2=$!
wait "$Q1" "$Q2"
W=$(jq -s '[.[0].status,.[1].status] | map(select(.=="committed"))|length' "$WORK/rb1.json" "$WORK/rb2.json")
C=$(jq -s '[.[0].status,.[1].status] | map(select(.=="conflict"))|length' "$WORK/rb1.json" "$WORK/rb2.json")
[[ "$W" == 1 && "$C" == 1 ]] && ok "并发回退：1 committed / 1 conflict" || bad "并发回退: $(cat "$WORK/rb1.json") $(cat "$WORK/rb2.json")"
api GET /v1/envs/staging; GEN_NOW=$(jq -r .gen <<<"$BODY")
[[ "$GEN_NOW" == 5 ]] && ok "并发回退后 gen=5" || bad "gen=$GEN_NOW"
for f in "$WORK/rb1.json" "$WORK/rb2.json"; do
  if [[ "$(jq -r .status "$f")" == committed ]]; then
    jq -c .receipt "$f" > "$WORK/rb-receipt.json"
    "$BIN/verifyreceipt" -f "$WORK/rb-receipt.json" >/dev/null && ok "回退收据验真通过" || bad "回退收据"
  fi
done

# GC 与“回退只能指向仍满足保留规则的版本”用独立环境 gc-env 演示：
# 按 v2 策略（keep_last_n=2）连续晋级 A/B/C，再 GC，d1 被物理回收。
info "GC + 回退限制：独立 gc-env，v2 策略 keep_last_n=2"
GENX=0
# 参数顺序: key digest evidence_id evidence_version（digest 含冒号，不能用 ':' 作分隔符）
gc_promote() {
  local key=$1 dg=$2 ev=$3 ver=$4
  sign_approval "apr-gc-$key" gc-env "$dg" "$ev" "$ver" "$GENX" > "$WORK/apr-gc-$key.json"
  api POST /v1/approvals "$(cat "$WORK/apr-gc-$key.json")"; expect_code 201 "gc-env 审批 $key gen=$GENX"
  api POST /v1/promotions "$(jq -nc --arg d "$dg" --arg e "$ev" --argjson v "$ver" --argjson g "$GENX" --arg a "apr-gc-$key" \
    '{env:"gc-env",digest:$d,evidence_id:$e,evidence_version:$v,policy_id:"promotion-policy","policy_version":2,expected_gen:$g,approval_id:$a}')"
  expect_field .status '"committed"' "gc-env 晋级 $key"
  GENX=$((GENX+1))
}
gc_promote a "$DA"  ev-a 2
gc_promote b "$DB_" ev-b 1
gc_promote c "$DC"  ev-c 1
api GET /v1/envs/gc-env; expect_field .gen 3 "gc-env gen=3"
api POST /v1/gc '{"env":"gc-env","policy_id":"promotion-policy","policy_version":2}'
expect_code 200 "GC 执行（v2 keep_last_n=2）"
echo "$BODY" > "$WORK/gc.json"
jq -e ".removed_blobs|length>=1" "$WORK/gc.json" >/dev/null \
  && ok "GC 物理删除了超出保留规则的副本（$(jq -r '.removed_blobs|length' "$WORK/gc.json") 个）" \
  || bad "GC 未删除: $BODY"
jq -e --arg c "$DC" '.skipped_inuse|index($c)' "$WORK/gc.json" >/dev/null \
  && ok "当前指针 C 在保留集内" || bad "当前指针被错误回收"
REMOVED=$(jq -r '.removed_blobs[0]' "$WORK/gc.json")
find "$BLOB/envs/gc-env" -type f -name "${REMOVED#sha256:}" | grep -q . \
  && bad "已 GC 的副本文件仍在磁盘" || ok "已 GC 副本物理文件已删除"

# 回退到被 GC 的 A → 409（历史存在但环境副本已不完整/不在保留集）
api POST /v1/rollbacks "$(jq -nc --arg d "$REMOVED" '{env:"gc-env",digest:$d,expected_gen:3,policy_id:"promotion-policy","policy_version":2}')"
expect_code 409 "回退被 GC 的 A 被拒（不满足保留规则/副本不完整）"

# 回退到保留集内的 B（gen2）成功
api POST /v1/rollbacks "$(jq -nc --arg d "$DB_" '{env:"gc-env",digest:$d,expected_gen:3,policy_id:"promotion-policy","policy_version":2}')"
expect_field .status '"committed"' "回退保留集内完整版本 B 成功"
api GET /v1/envs/gc-env; expect_field .current_digest "\"$DB_\"" "gc-env 指针回到 B"

# ---------- 9. 尝试审计 ----------
info "每次尝试都有完整证据"
api GET /v1/envs/staging/attempts?limit=100
TOTAL=$(jq '.attempts|length' <<<"$BODY")
FIN=$(jq '[.attempts[]|select(.finished_at!=null)]|length' <<<"$BODY")
[[ "$TOTAL" == "$FIN" ]] && ok "staging 全部 $TOTAL 次尝试均有终态证据" || bad "尝试未终结: $TOTAL vs $FIN"
psql "$DATABASE_URL" -tAc "SELECT string_agg(distinct status, ',') FROM promotion_attempts WHERE env='staging'" \
  | grep -q copy_failed && ok "证据中可查 copy_failed 尝试" || bad "无 copy_failed 证据"
psql "$DATABASE_URL" -tAc "SELECT count(*) FROM promotion_attempts WHERE copied_verified_digest IS NOT NULL AND env='staging'" \
  | awk '$1>=3{exit 0} {exit 1}' && ok "多次尝试记录了复制后重新哈希的摘要" || bad "复制校验摘要缺失"

# ---------- 汇总 ----------
echo
if [[ "$FAIL" == 0 ]]; then
  printf '%s================ ALL %d CHECKS PASSED ================%s\n' "$GREEN" "$PASS" "$NC"
  [[ -n "${KEEP_SERVER:-}" ]] || kill "$SRV_PID" 2>/dev/null || true
  exit 0
else
  printf '%s================ %d FAILED / %d passed ================%s\n' "$RED" "$FAIL" "$PASS" "$NC"
  echo "服务器日志: $WORK/server.log"
  exit 1
fi
