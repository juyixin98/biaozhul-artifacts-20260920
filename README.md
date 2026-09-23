# 哈希时间锁原子兑换（HTLC Atomic Swap）· Solidity + Foundry

在两条独立本地链上，用同一份哈希时间锁合约与两种测试 ERC-20 资产，演示经典的
**哈希时间锁原子兑换**：一方领取即公开原像，另一方凭原像领取；任一方不配合则到期各自
退款，资金不会跨链「一边到、一边不到」。

> ⚠️ **本项目是本地链教学/测试实现，不保证真实跨链原子性。** 详见文末
> [「安全模型与局限」](#安全模型与局限)。

---

## 1. 目录结构

```
.
├── foundry.toml              # 固定 solc 0.8.26 / lint / fmt 配置
├── src/
│   ├── HashTimeLock.sol      # HTLC 核心合约（状态机 + CEI + nonReentrant）
│   └── MockERC20.sol         # 测试资产（任何人可 mint，无外部依赖）
├── test/
│   ├── HashTimeLock.t.sol    # 单合约：锁/领/退、边界、终态、重复领取、错误原像
│   ├── AtomicSwap.t.sol      # 双链兑换时序：领取暴露原像、双方退款、安全间隔
│   ├── Reentrancy.t.sol      # 重入真实攻击：安全合约挡住 + UnsafeHTLC 对照被抽空
│   └── helpers/
│       ├── ReentrantERC20.sol  # 带接收回调的 ERC-20（触发重入）
│       ├── AttackerReceiver.sol# 攻击合约
│       └── UnsafeHTLC.sol      # 故意写坏的对照合约（无锁、违反 CEI）
├── script/
│   ├── Deploy.s.sol          # 单链部署脚本
│   └── replay.sh             # ★ 双 anvil 链端到端真实回放脚本
├── examples/
│   └── sample-inputs.json    # 示例输入（角色、金额、TTL、安全间隔、边界规则）
├── lib/forge-std/            # 固定 v1.9.7（commit 77041d2）的测试库源码
└── README.md
```

## 2. 协议约定（合约语义）

- **锁定绑定四要素**：`receiver`（唯一收款方）、`hashlock = keccak256(preimage)`、
  `amount`、`timelock`（Unix 秒），在 `lock()` 时一次性写入，之后不可改。
- **状态机（单向，只有一种中间态、两种互斥终态）**：

  ```
  ABSENT ──lock()──▶ LOCKED ──claim()──▶ CLAIMED   （终态）
                        │
                        └──refund()──▶ REFUNDED    （终态）
  ```

  `CLAIMED` 与 `REFUNDED` 互斥且不可逆；任何对终态锁的 `claim/refund` 都以
  `NotLocked()` 回滚。
- **时间边界（严格、互补）**：
  - 领取：`block.timestamp < timelock`（严格小于）；
  - 退款：`block.timestamp >= timelock`。
  - 因此 `timestamp == timelock` 这一刻：领取**必败**（`TooLate`），退款**可成**。
- **原像校验**：`keccak256(abi.encodePacked(preimage)) == hashlock`，不符则
  `WrongPreimage()`，状态保持 `LOCKED`、资金不动。
- **收款绑定**：任何人都能提交原像触发 `claim`（原像本就随事件公开），但资金**只**
  转给锁绑定的 `receiver`；退款恒退给 `sender`。
- **重入防护**：外部代币转账前先把状态置为终态（CEI），并加 `nonReentrant` 互斥锁。

### 安全间隔（两条腿的超时关系）

后手腿（B）的截止时间必须比先手腿（A）**严格长出一个明确的安全间隔**：

```
timelock_B = timelock_A + SAFETY_GAP
```

这样即使先手在 A 到期前最后 1 秒才领取并暴露原像，后手在「A 到期的同一墙钟时刻」
仍有完整 `SAFETY_GAP` 时间去 B 领取；后手未领取时，其退款窗口也严格晚于先手。
本项目演示取 `TTL_A = 100s`、`SAFETY_GAP = 60s`、`TTL_B = 160s`（真实部署中通常按
出块时间和中继延迟取分钟～小时级，且要考虑两条链的墙钟漂移）。

## 3. 环境要求与依赖锁定

- Linux/macOS，bash；`foundry`（forge/cast/anvil）、`jq`、`python3`、`openssl`、`curl`。
- Solidity 编译器：**固定 `0.8.26`**（`foundry.toml` 中 `solc = "0.8.26"`，首次构建
  Foundry 自动下载该精确版本）。
- 合约运行时**零第三方 Solidity 依赖**。
- 测试库 **forge-std 固定 `v1.9.7`（commit `77041d2ce690e692d6e03cc812b57d1ddaa4d505`）**，
  源码直接放在 `lib/forge-std/` 并随版本库提交，无需联网即可构建/测试。
- Foundry 自身版本：开发与验收使用 **1.8.3**。

安装 Foundry（若尚未安装）：

```bash
curl -L https://foundry.paradigm.xyz | bash
foundryup        # 安装 forge / cast / anvil
```

## 4. 本地启动与验收

### 4.1 一键验收（单元 + 攻击 + 双链时序，全自动）

```bash
# 1) 全部自动化测试（20 个用例，毫秒级）
forge test -vvv

# 2) 双本地链端到端真实回放（自动启停 anvil，约 10 秒，无需等待真实超时）
./script/replay.sh
```

`replay.sh` 真实执行：启动两条 anvil（chainid 31337/31338）→ `forge create`
真实部署 → `openssl` 随机生成 32 字节原像、`cast keccak` 真实算摘要 →
`cast send` 真实签名广播 mint/approve/lock/claim/refund → 从 `Claimed` 事件收据中
**真实解码原像**再用于对端领取；并用 `evm_setNextBlockTimestamp` 精确推进链时间来
覆盖到期边界。任何「本应回滚却成功」的步骤都会让脚本以非零码退出。

### 4.2 手工启动两条本地链并部署（可选）

```bash
anvil --chain-id 31337 --port 8545          # 终端 1：链 A
anvil --chain-id 31338 --port 8546          # 终端 2：链 B

# 终端 3
forge script script/Deploy.s.sol --rpc-url http://127.0.0.1:8545 --broadcast
TOKEN_NAME=TokenB TOKEN_SYMBOL=TKNB \
  forge script script/Deploy.s.sol --rpc-url http://127.0.0.1:8546 --broadcast
```

## 5. 测试覆盖清单（20 个用例）

| 类别 | 用例 |
|---|---|
| 锁定 | 事件与四要素正确、资金托管；零地址/零金额/过期时间回滚；相同参数 nonce 产生不同 id |
| 领取 | 正确原像在期前领取成功、资金只给绑定 receiver（他人可代交） |
| 负面 | **错误原像**（WrongPreimage，状态不变）；不存在的锁 |
| 终态 | **重复领取**回滚；已领取不能退款；**重复退款**回滚；已退款不能领取 |
| 时间边界 | **恰好到期**（`== timelock`）领取失败、同刻退款成功；到期前 1 秒可领 |
| 退款 | 到期前退款 TooEarly；到期后任何人可触发但款只退 sender |
| 兑换时序 | 快乐路径（A 领取暴露原像 → B 用原像领取）；无人配合**双方各自退款**；安全间隔下最后一刻暴露原像仍可在 B 领取；错误/过期原像失败 |
| 重入 | 恶意 ERC-20 回调中重入 claim / refund 均被 `ReentrantCall` 拒绝并整笔回滚；对照合约 UnsafeHTLC 被**真实抽空**（一把锁被领 4 次，400 代币被盗） |

重入对照的意义：同一份攻击代码，对 `src/HashTimeLock.sol` 失败、对故意写坏的
`UnsafeHTLC` 成功，实证防护有效而不是「测试空转」。

## 6. 示例输入

见 [`examples/sample-inputs.json`](examples/sample-inputs.json)：两条链 RPC/chainId、
Alice/Bob 地址、金额、`ttlA / safety_gap / ttlB`、截止边界规则，以及全部负面用例。

密码学操作均可手工复验（真实计算，不是写死常量）：

```bash
PREIMAGE=0x$(openssl rand -hex 32)
echo "$PREIMAGE"
cast keccak "$PREIMAGE"          # 即为链上使用的 hashlock
```

## 安全模型与局限（务必阅读）

1. **不保证真实跨链原子性。** 两条 anvil 链由同一脚本进程控制，原像在链下直接可得，
   脚本只演示**协议时序与状态机**。真实环境跨的是两条独立网络：
   - 原像要靠中继器/监听到 A 的 `Claimed` 事件后再在 B 广播；中继下线、交易被
     censorship、B 链拥堵都可能让后手虽然知道原像却无法在 `timelock_B` 前上链；
   - 两链墙钟相对漂移、出块确认深度不足（A 上「暴露原像」的交易所在区块被回滚）
     都会破坏「原子」直觉。安全间隔必须覆盖确认时间 + 中继延迟 + 时钟漂移。
2. **退款也只是「拿回自己的钱」，不是撤销对方已完成的领取。** 先手若已在 A 领取，
   后手必须在 B 领取；后手错过 B 窗口则既付出了 B 的资产又（通常）已在 A 被领走——
   这正是安全间隔必须足够大的原因。
3. `MockERC20.mint` 对任何人开放，**只能用于本地测试链**；`replay.sh` 用的是
   anvil 预置的公开测试私钥，切勿在任何真实网络使用。
4. 合约未做 ERC-20 fee-on-transfer / 余额在 lock 后变化等非标代币处理（测试资产是
   标准 1:1 ERC-20）。
5. 时间锁依赖 `block.timestamp`，出块者对时间戳有秒级操纵空间——对秒级 TTL 的演示
   足够，真实部署应使用显著大于出块间隔的期限。
