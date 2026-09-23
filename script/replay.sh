#!/usr/bin/env bash
# =============================================================================
# replay.sh —— 哈希时间锁原子兑换（HTLC）双本地链真实回放
#
# 真实执行内容（不是模拟打印）：
#   1) 用 anvil 启动两条独立本地链（chainid 31337 / 31338）；
#   2) 用 forge create 在两条链上真实部署 MockERC20 + HashTimeLock；
#   3) openssl 真实生成 32 字节随机原像，cast keccak 真实计算 keccak256 摘要；
#   4) cast send 真实签名/广播：铸造 -> approve -> lock 两条腿（TTL_A < TTL_B，
#      中间相差明确的 SAFETY_GAP 安全间隔）；
#   5) Bob 在链 A 领取，原像随 Claimed 事件上链，脚本从交易收据中真实解码原像，
#      Alice 在链 B 用该原像领取；
#   6) 负面回放：重复领取、错误原像、恰好到期（== timelock）、到期前退款、
#      未完成时双方各自退款、安全间隔保护最后一刻暴露原像的场景。
#
# 时间用 evm_setNextBlockTimestamp 精确推进，无需真实等待。
#
# ⚠️ 重要安全声明：
#   两条 anvil 链由同一进程控制，原像是脚本在链下直接拿到的。真实跨链环境中
#   「原像中继」需要中继器/事件监听+对端广播，且任一中继失败都会导致资金锁死
#   到退款；本脚本只在两条本地测试链上演示协议时序，不保证真实跨链原子性。
# =============================================================================
set -euo pipefail

# 让脚本在未手动 source foundry 环境时也能找到 forge/cast/anvil
export PATH="$PATH:${HOME}/.foundry/bin"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"
WORK_DIR="$(mktemp -d /tmp/htlc-demo.XXXXXX)"

# anvil 预置账户（公开测试密钥，仅本地）
PK_A='0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80' # Alice, account #0
PK_B='0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d' # Bob,   account #1
ALICE='0xf39Fd6e51aad88F6F4ce6ab8827279cfffb92266'
BOB='0x70997970C51812dc3A010C7d01b50e0d17dc79C8'

RPC_A='http://127.0.0.1:8545'
RPC_B='http://127.0.0.1:8546'
PORT_A=8545
PORT_B=8546
CID_A=31337
CID_B=31338

AMOUNT='100000000000000000000' # 100 代币（18 decimals）
TTL_A=100                      # 先手腿（链 A）锁定时长：100 秒
SAFETY_GAP=60                  # 明确安全间隔：60 秒
TTL_B=$((TTL_A + SAFETY_GAP))  # 后手腿（链 B）锁定时长：160 秒

LOCKED_SIG='Locked(bytes32,address,address,address,uint256,bytes32,uint256)'
CLAIMED_SIG='Claimed(bytes32,bytes32)'
LOCKED_TOPIC=$(cast keccak "$LOCKED_SIG")
CLAIMED_TOPIC=$(cast keccak "$CLAIMED_SIG")

ANVIL_A_PID=""
ANVIL_B_PID=""

c_info()  { printf '\033[1;36m[INFO]\033[0m  %s\n' "$*"; }
c_step()  { printf '\n\033[1;32m[STEP]\033[0m  %s\n' "$*"; }
c_ok()    { printf '\033[1;32m[ OK ]\033[0m  %s\n' "$*"; }
c_warn()  { printf '\033[1;33m[WARN]\033[0m  %s\n' "$*"; }
c_fail()  { printf '\033[1;31m[FAIL]\033[0m  %s\n' "$*"; }

cleanup() {
    c_info "清理：停止两条 anvil 链，删除临时目录 $WORK_DIR"
    [ -n "$ANVIL_A_PID" ] && kill "$ANVIL_A_PID" 2>/dev/null || true
    [ -n "$ANVIL_B_PID" ] && kill "$ANVIL_B_PID" 2>/dev/null || true
    rm -rf "$WORK_DIR"
}
trap cleanup EXIT

# 期待下一笔交易回滚；若居然成功则立即终止（说明安全约束失效）
expect_revert() {
    local desc="$1"; shift
    if "$@" >/dev/null 2>&1; then
        c_fail "$desc —— 本应回滚却成功了！"
        exit 1
    fi
    c_ok "$desc（按预期回滚）"
}

balance() { # balance <rpc> <token> <who>
    cast call "$2" 'balanceOf(address)(uint256)' "$3" --rpc-url "$1"
}

# 直接走 JSON-RPC（cast 1.8 的 --json 外层信封在不同子命令间不一致，直接 RPC 最稳）
# 参数类型前缀：i:123 -> 数字；b:true -> 布尔；其余原样作为字符串（如 latest / 0x..）
rpc() { # rpc <url> <method> [params...]
    local url="$1" method="$2"; shift 2
    local body
    body=$(python3 -c '
import json,sys
method, args = sys.argv[1], sys.argv[2:]
out = []
for a in args:
    if a.startswith("i:"):
        out.append(int(a[2:]))
    elif a.startswith("b:"):
        out.append(a[2:] == "true")
    else:
        out.append(a)
print(json.dumps({"jsonrpc":"2.0","id":1,"method":method,"params":out}))
' "$method" "$@")
    curl -s -X POST "$url" -H 'Content-Type: application/json' -d "$body" | jq -r '.result'
}

# anvil 语义：evm_setNextBlockTimestamp 只作用于「下一个区块」。
# 因此要让一笔交易在时刻 T 执行，只需设置后直接发交易（交易自身挖出该块），
# 切勿设置后再 evm_mine——否则交易会落到 T+1 的块里。
set_next_ts() { # set_next_ts <rpc> <target_ts>
    rpc "$1" evm_setNextBlockTimestamp "i:$2" >/dev/null
}

# 把「下一个区块」的时间设置为当前时间 + offset（不在此出块，留给后续交易）
warp() { # warp <rpc> <offset_seconds>
    local rpc="$1" offset="$2"
    set_next_ts "$rpc" $(( $(now_ts "$rpc") + offset ))
}

now_ts() {
    local hex
    hex=$(rpc "$1" eth_getBlockByNumber latest b:false | jq -r '.timestamp')
    printf '%d' "$((hex))" # bash 算术支持 0x 前缀十六进制
}

# 完整上锁流程：mint（资金来源测试铸造） -> approve -> lock，回显锁 ID
# 第 9 个参数为绝对截止时刻；不传则用 now + ttl（相对 TTL）
do_lock() { # do_lock <rpc> <token> <htlc> <sender_pk> <receiver> <amount> <hashlock> <ttl_or_abs_deadline> [abs]
    local rpc="$1" token="$2" htlc="$3" pk="$4" receiver="$5" amount="$6" hashlock="$7" ttl="$8" mode="${9:-rel}"
    local deadline receipt id
    if [ "$mode" = abs ]; then
        deadline=$ttl
    else
        deadline=$(( $(now_ts "$rpc") + ttl ))
    fi

    cast send "$token" 'mint(address,uint256)' "$(cast wallet address --private-key "$pk")" "$amount" \
        --rpc-url "$rpc" --private-key "$pk" >/dev/null
    cast send "$token" 'approve(address,uint256)' "$htlc" "$amount" \
        --rpc-url "$rpc" --private-key "$pk" >/dev/null

    # cast send --json 直接返回收据（含 logs）
    receipt=$(cast send "$htlc" 'lock(address,address,uint256,bytes32,uint256)' \
        "$receiver" "$token" "$amount" "$hashlock" "$deadline" \
        --rpc-url "$rpc" --private-key "$pk" --json)
    id=$(echo "$receipt" | jq -r --arg t "$LOCKED_TOPIC" '.logs[] | select(.topics[0]==$t) | .topics[1]')
    echo "$id $deadline"
}

# =============================================================================
c_step "0/9 编译合约"
forge build >/dev/null
c_ok "编译完成（solc 0.8.26）"

c_step "1/9 启动两条独立 anvil 本地链（chainid $CID_A 与 $CID_B）"
anvil --chain-id "$CID_A" --port "$PORT_A" --silent >"$WORK_DIR/anvilA.log" 2>&1 &
ANVIL_A_PID=$!
anvil --chain-id "$CID_B" --port "$PORT_B" --silent >"$WORK_DIR/anvilB.log" 2>&1 &
ANVIL_B_PID=$!
for rpc in "$RPC_A" "$RPC_B"; do
    for _ in $(seq 1 50); do
        cast block-number --rpc-url "$rpc" >/dev/null 2>&1 && break
        sleep 0.1
    done
done
c_ok "链 A: $RPC_A (chainid $(cast chain-id --rpc-url "$RPC_A"))"
c_ok "链 B: $RPC_B (chainid $(cast chain-id --rpc-url "$RPC_B"))"

# forge 1.8 create 的 --json 不含部署地址/哈希；直接从人类可读输出提取 "Deployed to: 0x..."
deploy() { # deploy <rpc> <pk> <ContractPath> [constructor args...]
    local rpc="$1" pk="$2" contract="$3"; shift 3
    forge create --skip compile --broadcast --rpc-url "$rpc" --private-key "$pk" "$contract" "$@" 2>&1 \
        | grep -oE 'Deployed to: 0x[0-9a-fA-F]{40}' | head -1 | awk '{print $3}'
}

c_step "2/9 两条链分别部署测试资产与 HTLC 合约"
TOKEN_A=$(deploy "$RPC_A" "$PK_A" src/MockERC20.sol:MockERC20 --constructor-args 'TokenA' 'TKNA')
HTLC_A=$(deploy "$RPC_A" "$PK_A" src/HashTimeLock.sol:HashTimeLock)
TOKEN_B=$(deploy "$RPC_B" "$PK_B" src/MockERC20.sol:MockERC20 --constructor-args 'TokenB' 'TKNB')
HTLC_B=$(deploy "$RPC_B" "$PK_B" src/HashTimeLock.sol:HashTimeLock)
c_info "链 A  TOKEN_A=$TOKEN_A  HTLC_A=$HTLC_A"
c_info "链 B  TOKEN_B=$TOKEN_B  HTLC_B=$HTLC_B"

c_step "3/9 密码学准备：openssl 随机生成 32 字节原像，cast keccak 计算摘要"
PREIMAGE='0x'"$(openssl rand -hex 32)"
HASHLOCK=$(cast keccak "$PREIMAGE")
c_info "preimage (秘密) = $PREIMAGE"
c_info "hashlock       = $HASHLOCK  = keccak256(preimage)"
[ "$(cast keccak "$PREIMAGE")" = "$HASHLOCK" ] && c_ok "摘要校验一致"

# =============================================================================
c_step "4/9 【快乐路径】双方上锁：A 腿 TTL=${TTL_A}s，B 腿 TTL=${TTL_B}s，安全间隔 SAFETY_GAP=${SAFETY_GAP}s"
read -r ID_A DEAD_A < <(do_lock "$RPC_A" "$TOKEN_A" "$HTLC_A" "$PK_A" "$BOB"   "$AMOUNT" "$HASHLOCK" "$TTL_A")
read -r ID_B DEAD_B < <(do_lock "$RPC_B" "$TOKEN_B" "$HTLC_B" "$PK_B" "$ALICE" "$AMOUNT" "$HASHLOCK" "$TTL_B")
c_info "链 A 锁 id=$ID_A 截止=$DEAD_A（Alice 锁 $AMOUNT wei TKNA 给 Bob）"
c_info "链 B 锁 id=$ID_B 截止=$DEAD_B（Bob   锁 $AMOUNT wei TKNB 给 Alice）"
[ "$(balance "$RPC_A" "$TOKEN_A" "$HTLC_A")" = "$AMOUNT" ] && c_ok "链 A 合约已托管 100 TKNA"
[ "$(balance "$RPC_B" "$TOKEN_B" "$HTLC_B")" = "$AMOUNT" ] && c_ok "链 B 合约已托管 100 TKNB"

c_step "5/9 负面用例（上锁后、领取前）：错误原像 / 到期前退款"
expect_revert "链 A 用错误原像领取（WrongPreimage）" \
    cast send "$HTLC_A" 'claim(bytes32,bytes32)' "$ID_A" \
    "0x$(printf '11%.0s' {1..32})" --rpc-url "$RPC_A" --private-key "$PK_B"
expect_revert "链 A 到期前退款（TooEarly）" \
    cast send "$HTLC_A" 'refund(bytes32)' "$ID_A" --rpc-url "$RPC_A" --private-key "$PK_A"
c_info "失败后链 A 锁状态仍为 LOCKED = $(cast call "$HTLC_A" 'stateOf(bytes32)(uint8)' "$ID_A" --rpc-url "$RPC_A")"

c_step "6/9 Bob 在链 A 截止前领取；脚本从 Claimed 事件收据中真实解码原像"
# 推进 30 秒（仍在 TTL_A=100 内）
warp "$RPC_A" 30
# cast send --json 即收据；Claimed(bytes32 indexed id, bytes32 preimage) 的 data 就是原像
CLAIM_RECEIPT=$(cast send "$HTLC_A" 'claim(bytes32,bytes32)' "$ID_A" "$PREIMAGE" \
    --rpc-url "$RPC_A" --private-key "$PK_B" --json)
REVEALED=$(echo "$CLAIM_RECEIPT" | jq -r --arg t "$CLAIMED_TOPIC" '.logs[] | select(.topics[0]==$t) | .data')
c_ok "Bob 已在链 A 领取，TKNA 余额 = $(balance "$RPC_A" "$TOKEN_A" "$BOB")"
c_info "从链 A 事件日志解码出的原像 = $REVEALED"
[ "$REVEALED" = "$PREIMAGE" ] && c_ok "事件中的原像与秘密一致（这正是跨链秘密暴露机制）"

expect_revert "链 A 重复领取同一把锁（NotLocked，终态不可逆）" \
    cast send "$HTLC_A" 'claim(bytes32,bytes32)' "$ID_A" "$PREIMAGE" --rpc-url "$RPC_A" --private-key "$PK_B"
expect_revert "链 A 已领取后再退款（NotLocked）" \
    cast send "$HTLC_A" 'refund(bytes32)' "$ID_A" --rpc-url "$RPC_A" --private-key "$PK_A"

c_step "7/9 Alice 用链 A 暴露的原像，在链 B 截止前领取"
warp "$RPC_B" 40
cast send "$HTLC_B" 'claim(bytes32,bytes32)' "$ID_B" "$REVEALED" \
    --rpc-url "$RPC_B" --private-key "$PK_A" >/dev/null
c_ok "Alice 已在链 B 领取，TKNB 余额 = $(balance "$RPC_B" "$TOKEN_B" "$ALICE")"
c_ok "快乐路径完成：Bob 拿到 100 TKNA，Alice 拿到 100 TKNB"

# =============================================================================
c_step "8/9 【退款路径】新开两把锁，无人领取 -> 到期各自退款（含恰好到期边界）"
read -r ID_A2 DEAD_A2 < <(do_lock "$RPC_A" "$TOKEN_A" "$HTLC_A" "$PK_A" "$BOB"   "$AMOUNT" "$HASHLOCK" "$TTL_A")
read -r ID_B2 DEAD_B2 < <(do_lock "$RPC_B" "$TOKEN_B" "$HTLC_B" "$PK_B" "$ALICE" "$AMOUNT" "$HASHLOCK" "$TTL_B")
c_info "退款场景锁：A=$ID_A2 (截止 $DEAD_A2)  B=$ID_B2 (截止 $DEAD_B2)"

# 链 A：让下一笔交易的区块时间恰好等于 deadline（== 而非 >）
set_next_ts "$RPC_A" "$DEAD_A2"
c_info "链 A 下一区块时间将设为 = $DEAD_A2，timelock = $DEAD_A2（恰好相等）"
expect_revert "链 A 恰好到期瞬间领取（TooLate：claim 要求严格小于）" \
    cast send "$HTLC_A" 'claim(bytes32,bytes32)' "$ID_A2" "$PREIMAGE" --rpc-url "$RPC_A" --private-key "$PK_B"
# 失败的 claim 已挖出一个 == deadline 的块；退款交易同样要在 deadline 时刻发
set_next_ts "$RPC_A" "$DEAD_A2"
cast send "$HTLC_A" 'refund(bytes32)' "$ID_A2" --rpc-url "$RPC_A" --private-key "$PK_A" >/dev/null
c_ok "链 A 恰好到期时退款成功，Alice TKNA 余额 = $(balance "$RPC_A" "$TOKEN_A" "$ALICE")（拿回自己锁的 100）"
expect_revert "链 A 重复退款（NotLocked）" \
    cast send "$HTLC_A" 'refund(bytes32)' "$ID_A2" --rpc-url "$RPC_A" --private-key "$PK_A"

# 链 B：此刻未到期（安全间隔内），不能提前退款
expect_revert "链 B 尚未到期，提前退款（TooEarly）" \
    cast send "$HTLC_B" 'refund(bytes32)' "$ID_B2" --rpc-url "$RPC_B" --private-key "$PK_B"
# 让退款交易在 B 的截止时刻之后 1 秒执行
set_next_ts "$RPC_B" $((DEAD_B2 + 1))
cast send "$HTLC_B" 'refund(bytes32)' "$ID_B2" --rpc-url "$RPC_B" --private-key "$PK_B" >/dev/null
c_ok "链 B 到期后退款成功，Bob TKNB 余额 = $(balance "$RPC_B" "$TOKEN_B" "$BOB")（拿回自己锁的 100）"

# =============================================================================
c_step "9/9 【安全间隔演示】最坏情况：Bob 在链 A 到期前 1 秒才暴露原像"
# 两条腿用同一墙钟基准 + 明确绝对截止时刻，避免跨链依次上锁的时钟漂移影响论证
BASE_TS=$(now_ts "$RPC_A")
DEAD_A3=$((BASE_TS + TTL_A))
DEAD_B3=$((DEAD_A3 + SAFETY_GAP))
read -r ID_A3 _ < <(do_lock "$RPC_A" "$TOKEN_A" "$HTLC_A" "$PK_A" "$BOB"   "$AMOUNT" "$HASHLOCK" "$DEAD_A3" abs)
read -r ID_B3 _ < <(do_lock "$RPC_B" "$TOKEN_B" "$HTLC_B" "$PK_B" "$ALICE" "$AMOUNT" "$HASHLOCK" "$DEAD_B3" abs)
c_info "绝对截止：A=$DEAD_A3，B=$DEAD_B3（B 严格晚 $SAFETY_GAP 秒）"

# A：让 Bob 的领取交易在「到期前 1 秒」的区块中执行（最后一刻暴露原像）
set_next_ts "$RPC_A" $((DEAD_A3 - 1))
cast send "$HTLC_A" 'claim(bytes32,bytes32)' "$ID_A3" "$PREIMAGE" \
    --rpc-url "$RPC_A" --private-key "$PK_B" >/dev/null
c_ok "Bob 在链 A 到期前 1 秒领取成功（原像在最后一刻暴露）"

# 关键安全论证：假设 Alice 的对端交易在「链 A 到期的同一墙钟时刻」上链。
# 此刻 B 恰好还剩完整 SAFETY_GAP —— 这就是后手腿必须更长的原因。
REMAIN=$((DEAD_B3 - DEAD_A3))
c_info "在链 A 到期的同一墙钟时刻，链 B 距自己截止还剩 $REMAIN 秒（= SAFETY_GAP=${SAFETY_GAP}s）"
if [ "$REMAIN" -lt "$SAFETY_GAP" ]; then
    c_fail "安全间隔不足！后手腿将无法领取"; exit 1
fi
c_ok "安全间隔保证：先手腿到期的瞬间，后手腿仍有完整间隔可领取"

# Alice 一直拖到 B 到期前 1 秒才领取，仍成功
set_next_ts "$RPC_B" $((DEAD_B3 - 1))
cast send "$HTLC_B" 'claim(bytes32,bytes32)' "$ID_B3" "$PREIMAGE" \
    --rpc-url "$RPC_B" --private-key "$PK_A" >/dev/null
c_ok "Alice 在链 B 截止前 1 秒凭原像领取成功"

# =============================================================================
printf '\n'
c_ok "全部回放步骤完成。最终状态（三轮上锁后的链上真实余额，单位 wei）："
printf '   链 A: Bob   TKNA = %s  (200 代币：第1、3轮领取；第2轮到期未领)\n' "$(balance "$RPC_A" "$TOKEN_A" "$BOB")"
printf '   链 A: Alice TKNA = %s  (100 代币：铸 300 - 第1轮付Bob 100 + 第2轮退款 100 - 第3轮付Bob 100)\n' "$(balance "$RPC_A" "$TOKEN_A" "$ALICE")"
printf '   链 B: Alice TKNB = %s  (200 代币：第1、3轮领取)\n' "$(balance "$RPC_B" "$TOKEN_B" "$ALICE")"
printf '   链 B: Bob   TKNB = %s  (100 代币：铸 300 - 两轮被领走 + 第2轮退款 100)\n' "$(balance "$RPC_B" "$TOKEN_B" "$BOB")"
printf '\n'
c_warn "提醒：本演示不保证真实跨链原子性。详见 README「安全模型与局限」。"
