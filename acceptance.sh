#!/usr/bin/env bash
# 一键验收：构建（release）→ 真实证明 → 四类对抗用例 → 假收据隔离。
# 失败即退出（set -e），每一步打印明确结论。
set -u
cd "$(dirname "$0")"

BIN="cargo run --release -q --"
PASS=0; FAIL=0

ok()   { printf '\n\033[32m[PASS]\033[0m %s\n' "$1"; PASS=$((PASS+1)); }
bad()  { printf '\n\033[31m[FAIL]\033[0m %s\n' "$1"; FAIL=$((FAIL+1)); }
step() { printf '\n==== %s ====\n' "$1"; }

step "构建 release（真实证明能力）"
cargo build --release -q || { echo "构建失败"; exit 1; }
BIN=./target/release/batch-median

step "1. 执行（不出证明）"
$BIN execute examples/batch.json > /tmp/bm_exec.json
grep -q '"median": 9' /tmp/bm_exec.json && ok "execution median=9" || bad "execution"

step "2. 真实 STARK 证明 + 验证（核心验收项，可能耗时数十秒）"
$BIN prove examples/batch.json --receipt-out /tmp/bm.receipt.json > /tmp/bm_prove.json
if grep -q '"verified": true' /tmp/bm_prove.json \
   && grep -q 'composite-stark' /tmp/bm_prove.json; then
  ok "真实证明已验证 (composite-stark)"
  grep -E 'execution_ms|proving_ms|verification_ms' /tmp/bm_prove.json
else
  bad "真实证明未通过"; cat /tmp/bm_prove.json; exit 1
fi

step "3. 离线重新验证真实收据"
$BIN verify /tmp/bm.receipt.json > /tmp/bm_verify.json
grep -q '"verified": true' /tmp/bm_verify.json && ok "重新验证通过" || bad "重新验证"

step "4. 更换程序 ID（必须拒绝，退出码 2）"
ZERO=0000000000000000000000000000000000000000000000000000000000000000
set +e
$BIN verify /tmp/bm.receipt.json --expect-image-id "$ZERO" > /tmp/bm_id.json 2>/dev/null
RC=$?
set -e
[ "$RC" = "2" ] && grep -q '"verified": false' /tmp/bm_id.json \
  && ok "更换 image ID 被拒绝 (exit=2)" || bad "更换 image ID 未被拒绝 (rc=$RC)"

step "5. 篡改 journal（必须拒绝，退出码 2）"
set +e
$BIN verify /tmp/bm.receipt.json --tamper-journal > /tmp/bm_tamper.json 2>/dev/null
RC=$?
set -e
[ "$RC" = "2" ] && grep -q '"verified": false' /tmp/bm_tamper.json \
  && ok "篡改 journal 被拒绝 (exit=2)" || bad "篡改 journal 未被拒绝 (rc=$RC)"

step "6. 超范围输入（宿主预检失败，退出码 1）"
for f in out_of_range out_of_range_neg; do
  set +e; $BIN execute examples/$f.json >/dev/null 2>&1; RC=$?; set -e
  [ "$RC" = "1" ] && ok "$f 被拒绝" || bad "$f 未被拒绝 (rc=$RC)"
done

step "7. 空批次（退出码 1）"
set +e; $BIN execute examples/empty.json >/dev/null 2>&1; RC=$?; set -e
[ "$RC" = "1" ] && ok "空批次被拒绝" || bad "空批次未被拒绝 (rc=$RC)"

step "8. 客体自身强制规则（绕过宿主预检仍失败）"
set +e
$BIN execute examples/out_of_range.json --unsafe-skip-preflight >/dev/null 2>&1; RC1=$?
$BIN execute examples/empty.json --unsafe-skip-preflight >/dev/null 2>&1; RC2=$?
set -e
[ "$RC1" = "1" ] && [ "$RC2" = "1" ] \
  && ok "guest 内范围/空批次校验生效" || bad "guest 校验未生效 ($RC1,$RC2)"

step "9. dev 假收据隔离"
$BIN --dev-mode prove examples/single.json --receipt-out /tmp/bm_fake.json >/dev/null 2>&1
set +e
$BIN verify /tmp/bm_fake.json > /tmp/bm_fake_verify.json 2>/dev/null; RC=$?
set -e
[ "$RC" = "2" ] && grep -q 'fake-dev-only' /tmp/bm_fake_verify.json \
  && ok "假收据被默认验证拒绝" || bad "假收据竟然通过了 (rc=$RC)"
set +e
$BIN --dev-mode verify /tmp/bm_fake.json > /tmp/bm_fake_dev.json 2>/dev/null; RC=$?
set -e
[ "$RC" = "0" ] && grep -q '"verified": true' /tmp/bm_fake_dev.json \
  && grep -q 'fake-dev-only' /tmp/bm_fake_dev.json \
  && ok "显式 --dev-mode 才接受且明确标记 FAKE" || bad "dev opt-in 行为异常 (rc=$RC)"

step "10. 自动化测试（快速套件）"
cargo test -q -p batch-median-host --test e2e >/tmp/bm_test.log 2>&1 \
  && ok "12 个快速测试通过" || { bad "测试失败"; tail -30 /tmp/bm_test.log; FAIL=$((FAIL+1)); }

printf '\n================ 结果: %d 通过, %d 失败 ================\n' "$PASS" "$FAIL"
[ "$FAIL" = 0 ]
