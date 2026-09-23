#!/usr/bin/env bash
# =============================================================================
# 哈希时间锁（HTLC）原子兑换 —— 双本地链回放脚本
#
# 本脚本会：
#   1. 启动两条相互独立的 anvil 本地区块链（A: 8545 / B: 8546）
#   2. 在两条链上分别部署 TestToken 与 HashTimeLock（真实部署、真实交易）
#   3. 场景一（成功兑换）：
#        Alice 在 A 链锁 100 TKA 给 Bob（长超时 TA）
#        Bob   在 B 链锁  50 TKB 给 Alice（短超时 TB，TA = TB + 安全间隔）
#        Alice 在 B 链出示原像领取 -> 脚本“从收据日志中真实提取原像”
#        Bob   用该原像在 A 链领取
#   4. 负面用例：错误原像、重复领取、到期前退款、终态后再领取（均应链上回滚）
#   5. 场景二（放弃兑换）：两把新锁都不领取，各自推进链上时间，到期分别退款
#
# 原像由 openssl 现场随机生成（或由环境变量提供），摘要由 openssl sha256 真实计算。
# 注意：这是两台独立进程的演示，不包含中继/头验证，不保证真实跨链原子性，见 README。
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
export PATH="$HOME/.foundry/bin:$PATH"

# -----------------------------------------------------------------------------
# 参数（可被环境变量或 script/demo.env 覆盖）——示例输入见 demo.env.example
# -----------------------------------------------------------------------------
if [[ -f script/demo.env ]]; then
  # shellcheck disable=SC1091
  source script/demo.env
fi

RPC_A="${RPC_A:-http://127.0.0.1:8545}"
RPC_B="${RPC_B:-http://127.0.0.1:8546}"
CHAIN_A_ID="${CHAIN_A_ID:-31337}"
CHAIN_B_ID="${CHAIN_B_ID:-31338}"

# anvil 预置账户（纯本地测试，私钥公开是正常的）
ALICE_PK="${ALICE_PK:-0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80}"
BOB_PK="${BOB_PK:-0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d}"
ALICE="0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
BOB="0x70997970C51812dc3A010C7d01b50e0d17dc79C8"

AMOUNT_A="${AMOUNT_A:-100000000000000000000}"   # 100 TKA
AMOUNT_B="${AMOUNT_B:-50000000000000000000}"     #  50 TKB
SECONDS_TO_TB="${SECONDS_TO_TB:-120}"            # B 链锁在现在 +120s 到期
SAFETY_MARGIN="${SAFETY_MARGIN:-300}"            # 明确安全间隔：A 比 B 晚 300s
SECONDS_TO_TB2="${SECONDS_TO_TB2:-40}"           # 退款场景的 B 链锁
SAFETY_MARGIN2="${SAFETY_MARGIN2:-120}"          # 退款场景的安全间隔

# 原像：默认现场生成 32 字节随机数（真实密码学随机源）
SECRET_HEX="${SECRET_HEX:-$(openssl rand -hex 32)}"
SECRET_HEX="${SECRET_HEX#0x}"

RUN_DIR="$ROOT/.run"
mkdir -p "$RUN_DIR"

# -----------------------------------------------------------------------------
# 输出辅助
# -----------------------------------------------------------------------------
c_green=$'\033[32m'; c_red=$'\033[31m'; c_cyan=$'\033[36m'; c_bold=$'\033[1m'; c_off=$'\033[0m'
step() { printf '\n%s==> %s%s\n' "$c_cyan$c_bold" "$*" "$c_off"; }
ok()   { printf '%s[OK]%s %s\n' "$c_green" "$c_off" "$*"; }
die()  { printf '%s[FAIL]%s %s\n' "$c_red" "$c_off" "$*" >&2; exit 1; }

# 期望交易被链上拒绝（回滚）；若居然成功则失败退出
expect_revert() {
  local desc="$1"; shift
  if "$@" >/dev/null 2>&1; then
    die "$desc —— 预期回滚，但交易成功了"
  fi
  ok "$desc（已按预期回滚）"
}

assert_eq() {
  local desc="$1" got="$2" want="$3"
  [[ "$got" == "$want" ]] || die "$desc：期望 $want，实际 $got"
  ok "$desc = $got"
}

cleanup() {
  [[ -n "${ANVIL_A_PID:-}" ]] && kill "$ANVIL_A_PID" 2>/dev/null || true
  [[ -n "${ANVIL_B_PID:-}" ]] && kill "$ANVIL_B_PID" 2>/dev/null || true
}
trap cleanup EXIT

wait_rpc() {
  local rpc="$1" n=0
  until cast block-number --rpc-url "$rpc" >/dev/null 2>&1; do
    n=$((n + 1)); [[ $n -lt 50 ]] || die "等待 $rpc 就绪超时"
    sleep 0.2
  done
}

advance() { # advance <rpc> <秒> ：推进该链时钟并出块
  cast rpc evm_increaseTime "$2" --rpc-url "$1" >/dev/null
  cast rpc evm_mine --rpc-url "$1" >/dev/null
}

# 对十六进制原像的【原始字节】计算 SHA-256（新版 cast 无 sha256 子命令，
# 故用 xxd 还原为字节后交给 openssl，与 Solidity 的 sha256(bytes) 语义一致）
sha256_hex() {
  local h="${1#0x}"
  printf '%s' "$h" | xxd -r -p | openssl dgst -sha256 -r | cut -d' ' -f1 | sed 's/^/0x/'
}

# 读取链上状态（cast 1.8 普通输出会附 "[5e19]" 之类的人类可读后缀，用 --json 取首元素）
state_of() { # state_of <rpc> <htlc> <lockId>
  cast call "$2" 'getState(uint256)(uint8)' "$3" --rpc-url "$1" --json | jq -r '.[0]'
}
bal_of() { # bal_of <rpc> <token> <holder>
  cast call "$2" 'balanceOf(address)(uint256)' "$3" --rpc-url "$1" --json | jq -r '.[0]'
}

# -----------------------------------------------------------------------------
# 0. 前置检查与原像/摘要（真实密码学操作）
# -----------------------------------------------------------------------------
command -v openssl >/dev/null || die "缺少 openssl"
command -v forge >/dev/null   || die "找不到 forge，请先安装 Foundry 并加入 PATH"
[[ ${#SECRET_HEX} -eq 64 ]] || die "SECRET_HEX 必须是 32 字节（64 个十六进制字符）"

step "0. 密码学准备"
HASH="$(sha256_hex "$SECRET_HEX")"
echo "    原像 secret(32B) = 0x$SECRET_HEX"
echo "    摘要 sha256      = $HASH"
cast keccak "Claimed(uint256,bytes)" >"$RUN_DIR/claimed.topic"
echo "    Claimed 事件 topic0 = $(cat "$RUN_DIR/claimed.topic")"

# -----------------------------------------------------------------------------
# 1. 启动两条独立本地链
# -----------------------------------------------------------------------------
step "1. 启动两条独立 anvil 本地区块链（A=:$CHAIN_A_ID / B=:$CHAIN_B_ID）"
anvil --chain-id "$CHAIN_A_ID" --port 8545 --silent >"$RUN_DIR/anvil-a.log" 2>&1 &
ANVIL_A_PID=$!
anvil --chain-id "$CHAIN_B_ID" --port 8546 --silent >"$RUN_DIR/anvil-b.log" 2>&1 &
ANVIL_B_PID=$!
wait_rpc "$RPC_A"; wait_rpc "$RPC_B"
ok "链 A 就绪：$RPC_A (pid $ANVIL_A_PID)"
ok "链 B 就绪：$RPC_B (pid $ANVIL_B_PID)"

# -----------------------------------------------------------------------------
# 2. 两条链分别部署 TestToken + HashTimeLock（真实部署）
# -----------------------------------------------------------------------------
step "2. 部署合约"
# forge v1.8.3：内联 --constructor-args 对多参数解析异常，使用 --constructor-args-path；
# 且需显式 --broadcast 才会真正发交易。
printf 'TokenA\nTKA\n' >"$RUN_DIR/args-a.txt"
printf 'TokenB\nTKB\n' >"$RUN_DIR/args-b.txt"
TKA=$(forge create src/TestToken.sol:TestToken --rpc-url "$RPC_A" --private-key "$ALICE_PK" \
        --constructor-args-path "$RUN_DIR/args-a.txt" --broadcast --json | jq -r .deployedTo)
HTLC_A=$(forge create src/HashTimeLock.sol:HashTimeLock --rpc-url "$RPC_A" --private-key "$ALICE_PK" --broadcast --json | jq -r .deployedTo)
TKB=$(forge create src/TestToken.sol:TestToken --rpc-url "$RPC_B" --private-key "$ALICE_PK" \
        --constructor-args-path "$RUN_DIR/args-b.txt" --broadcast --json | jq -r .deployedTo)
HTLC_B=$(forge create src/HashTimeLock.sol:HashTimeLock --rpc-url "$RPC_B" --private-key "$ALICE_PK" --broadcast --json | jq -r .deployedTo)
cat >"$RUN_DIR/addresses.env" <<EOF
TKA=$TKA
HTLC_A=$HTLC_A
TKB=$TKB
HTLC_B=$HTLC_B
EOF
ok "链 A：TestToken $TKA / HashTimeLock $HTLC_A"
ok "链 B：TestToken $TKB / HashTimeLock $HTLC_B"

# -----------------------------------------------------------------------------
# 3. 铸造测试资产并授权 HTLC 托管
# -----------------------------------------------------------------------------
step "3. 铸造测试资产并 approve 给各自链上的 HTLC"
cast send "$TKA" "mint(address,uint256)" "$ALICE" "$AMOUNT_A" --rpc-url "$RPC_A" --private-key "$ALICE_PK" >/dev/null
cast send "$TKA" "approve(address,uint256)" "$HTLC_A" "115792089237316195423570985008687907853269984665640564039457584007913129639935" --rpc-url "$RPC_A" --private-key "$ALICE_PK" >/dev/null
cast send "$TKB" "mint(address,uint256)" "$BOB" "$AMOUNT_B" --rpc-url "$RPC_B" --private-key "$ALICE_PK" >/dev/null
cast send "$TKB" "approve(address,uint256)" "$HTLC_B" "115792089237316195423570985008687907853269984665640564039457584007913129639935" --rpc-url "$RPC_B" --private-key "$BOB_PK" >/dev/null
ok "Alice 持有 100 TKA 并授权；Bob 持有 50 TKB 并授权"

NOW_A=$(cast block latest --rpc-url "$RPC_A" -f timestamp)
NOW_B=$(cast block latest --rpc-url "$RPC_B" -f timestamp)
TB=$((NOW_B + SECONDS_TO_TB))
TA=$((TB + SAFETY_MARGIN))   # 安全间隔：A 链截止严格晚于 B 链
TB2=$((NOW_B + SECONDS_TO_TB2))
TA2=$((TB2 + SAFETY_MARGIN2))
echo "    B 链截止 TB = $TB（now_B+$SECONDS_TO_TB）"
echo "    A 链截止 TA = $TA（TB + 安全间隔 $SAFETY_MARGIN）"

# -----------------------------------------------------------------------------
# 4. 场景一：双方锁定
# -----------------------------------------------------------------------------
step "4. 场景一 · 双方锁定（同一 sha256 摘要、不同金额/方向/截止时间）"
cast send "$HTLC_A" "lock(address,address,uint256,bytes32,uint64)" \
  "$BOB" "$TKA" "$AMOUNT_A" "$HASH" "$TA" --rpc-url "$RPC_A" --private-key "$ALICE_PK" >/dev/null
cast send "$HTLC_B" "lock(address,address,uint256,bytes32,uint64)" \
  "$ALICE" "$TKB" "$AMOUNT_B" "$HASH" "$TB" --rpc-url "$RPC_B" --private-key "$BOB_PK" >/dev/null
assert_eq "A 锁 #1 状态(1=LOCKED)" "$(state_of "$RPC_A" "$HTLC_A" 1)" "1"
assert_eq "B 锁 #1 状态(1=LOCKED)" "$(state_of "$RPC_B" "$HTLC_B" 1)" "1"

step "5. 负面用例（领取窗口内）"
expect_revert "B 链错误原像领取" cast send "$HTLC_B" "claim(uint256,bytes)" 1 0xdeadbeef \
  --rpc-url "$RPC_B" --private-key "$ALICE_PK"
expect_revert "B 链非接收者领取" cast send "$HTLC_B" "claim(uint256,bytes)" 1 "0x$SECRET_HEX" \
  --rpc-url "$RPC_B" --private-key "$BOB_PK"
expect_revert "A 链未到期退款" cast send "$HTLC_A" "refund(uint256)" 1 \
  --rpc-url "$RPC_A" --private-key "$ALICE_PK"
assert_eq "失败后 A 锁仍为 LOCKED" "$(state_of "$RPC_A" "$HTLC_A" 1)" "1"

# -----------------------------------------------------------------------------
# 6. Alice 在 B 链领取，原像从收据日志中真实提取
# -----------------------------------------------------------------------------
step "6. Alice 在 B 链出示原像领取 50 TKB"
CLAIM_B_TX=$(cast send "$HTLC_B" "claim(uint256,bytes)" 1 "0x$SECRET_HEX" \
  --rpc-url "$RPC_B" --private-key "$ALICE_PK" --json | jq -r .transactionHash)
assert_eq "B 锁 #1 状态(2=CLAIMED)" "$(state_of "$RPC_B" "$HTLC_B" 1)" "2"

# 从收据 logs 中解码 Claimed 事件 data（ABI 编码的 bytes：偏移32B | 长度32B | 原像）
LOG_DATA=$(cast receipt "$CLAIM_B_TX" --rpc-url "$RPC_B" --json \
  | jq -r --arg topic "$(cat "$RUN_DIR/claimed.topic")" \
    '.logs[] | select(.topics[0]==$topic) | .data')
SECRET_LEN=$((16#$(printf '%s' "$LOG_DATA" | cut -c67-130)))
SECRET_FROM_LOG="0x$(printf '%s' "$LOG_DATA" | cut -c131-$((130 + 2 * SECRET_LEN)))"
echo "    从 B 链收据日志提取到原像 = $SECRET_FROM_LOG（长度 $SECRET_LEN 字节）"
[[ "$SECRET_FROM_LOG" == "0x$SECRET_HEX" ]] || die "日志提取的原像与本地原像不一致"
assert_eq "链上提取原像的 sha256" "$(sha256_hex "$SECRET_FROM_LOG")" "$HASH"
ok "原像一致性验证通过（这就是跨链传递原像的真实机制：公开日志）"

expect_revert "B 链重复领取" cast send "$HTLC_B" "claim(uint256,bytes)" 1 "$SECRET_FROM_LOG" \
  --rpc-url "$RPC_B" --private-key "$ALICE_PK"
assert_eq "Alice 收到 50 TKB" \
  "$(bal_of "$RPC_B" "$TKB" "$ALICE")" "$AMOUNT_B"

# -----------------------------------------------------------------------------
# 7. Bob 用从 B 链得到的原像，在 A 链领取
# -----------------------------------------------------------------------------
step "7. Bob 用日志中的原像在 A 链领取 100 TKA"
cast send "$HTLC_A" "claim(uint256,bytes)" 1 "$SECRET_FROM_LOG" \
  --rpc-url "$RPC_A" --private-key "$BOB_PK" >/dev/null
assert_eq "A 锁 #1 状态(2=CLAIMED)" "$(state_of "$RPC_A" "$HTLC_A" 1)" "2"
assert_eq "Bob 收到 100 TKA" \
  "$(bal_of "$RPC_A" "$TKA" "$BOB")" "$AMOUNT_A"
assert_eq "A 链 HTLC 托管余额归零" \
  "$(cast call "$TKA" 'balanceOf(address)(uint256)' "$HTLC_A" --rpc-url "$RPC_A")" "0"
expect_revert "CLAIMED 后再退款" cast send "$HTLC_A" "refund(uint256)" 1 \
  --rpc-url "$RPC_A" --private-key "$ALICE_PK"
ok "场景一完成：兑换成功，两边都是 CLAIMED 终态"

# -----------------------------------------------------------------------------
# 8. 场景二：放弃兑换，双方各自到期退款
# -----------------------------------------------------------------------------
step "8. 场景二 · 新建两把锁但都不领取，到期各自退款（TB2=$TB2, TA2=$TA2）"
cast send "$TKA" "mint(address,uint256)" "$ALICE" "$AMOUNT_A" --rpc-url "$RPC_A" --private-key "$ALICE_PK" >/dev/null
cast send "$HTLC_A" "lock(address,address,uint256,bytes32,uint64)" \
  "$BOB" "$TKA" "$AMOUNT_A" "$HASH" "$TA2" --rpc-url "$RPC_A" --private-key "$ALICE_PK" >/dev/null
cast send "$TKB" "mint(address,uint256)" "$BOB" "$AMOUNT_B" --rpc-url "$RPC_B" --private-key "$ALICE_PK" >/dev/null
cast send "$HTLC_B" "lock(address,address,uint256,bytes32,uint64)" \
  "$ALICE" "$TKB" "$AMOUNT_B" "$HASH" "$TB2" --rpc-url "$RPC_B" --private-key "$BOB_PK" >/dev/null
assert_eq "A 锁 #2 状态 LOCKED" "$(state_of "$RPC_A" "$HTLC_A" 2)" "1"
assert_eq "B 锁 #2 状态 LOCKED" "$(state_of "$RPC_B" "$HTLC_B" 2)" "1"

# B 链先到期（短超时）：恰好到期的瞬间——claim 已失效，refund 才生效，且仅发送者可退
advance "$RPC_B" "$SECONDS_TO_TB2"
expect_revert "B 链恰好到期(t==timelock)领取仍失败" cast send "$HTLC_B" "claim(uint256,bytes)" 2 "0x$SECRET_HEX" \
  --rpc-url "$RPC_B" --private-key "$ALICE_PK"
expect_revert "B 链非发送者退款" cast send "$HTLC_B" "refund(uint256)" 2 \
  --rpc-url "$RPC_B" --private-key "$ALICE_PK"
cast send "$HTLC_B" "refund(uint256)" 2 --rpc-url "$RPC_B" --private-key "$BOB_PK" >/dev/null
assert_eq "B 锁 #2 状态(3=REFUNDED)" "$(state_of "$RPC_B" "$HTLC_B" 2)" "3"
expect_revert "REFUNDED 后再领取" cast send "$HTLC_B" "claim(uint256,bytes)" 2 "0x$SECRET_HEX" \
  --rpc-url "$RPC_B" --private-key "$ALICE_PK"

# A 链后到期（安全间隔之后）：Alice 退款
advance "$RPC_A" "$((SECONDS_TO_TB2 + SAFETY_MARGIN2))"
cast send "$HTLC_A" "refund(uint256)" 2 --rpc-url "$RPC_A" --private-key "$ALICE_PK" >/dev/null
assert_eq "A 锁 #2 状态(3=REFUNDED)" "$(state_of "$RPC_A" "$HTLC_A" 2)" "3"
assert_eq "Alice 收回 100 TKA（场景二这笔完璧归赵）" \
  "$(bal_of "$RPC_A" "$TKA" "$ALICE")" "$AMOUNT_A"
assert_eq "Bob 收回 50 TKB（场景二这笔完璧归赵）" \
  "$(bal_of "$RPC_B" "$TKB" "$BOB")" "$AMOUNT_B"

# -----------------------------------------------------------------------------
# 9. 汇总
# -----------------------------------------------------------------------------
step "9. 最终状态汇总"
printf '    %-10s %-12s %-12s\n' "链/锁" "场景一(#1)" "场景二(#2)"
printf '    %-10s %-12s %-12s\n' "A" "CLAIMED(2)" "REFUNDED(3)"
printf '    %-10s %-12s %-12s\n' "B" "CLAIMED(2)" "REFUNDED(3)"
echo
ok "全部回放步骤通过。合约地址与运行日志保存在 .run/（脚本退出时自动关闭 anvil）"
