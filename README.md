# 合约事件不变量测试：流式归属合约（VestingStream）

纯后端 Solidity + Foundry 项目：实现资金按时间线性归属的流式归属合约，
并用**随机操作序列 + 独立模型**的状态机不变量测试核对余额、事件与可领取额。

## 功能与规则

- **线性归属**：`vested(t) = amount * (t - start) / (end - start)`（`cliff <= t < end`）；
  `t < cliff` 时为 0；`t >= end` 时为全额 `amount` —— 整数除法余量在 `end` 时刻结清。
- **撤销（cancel）**：仅发送者（sender）可调用；仅收回**未归属**部分退回 sender，
  已归属未领取额度永久保留给受益人（beneficiary），撤销后归属时间冻结在 `canceledAt`。
- **领取（claim）**：仅受益人可调用，领走全部已归属未领取额度；无可领额度时 revert。
- **资金补足（topUp）**：仅 sender，未撤销的流可追加资金，归属曲线按新总额重算。
- **资产守恒**：合约托管余额 == Σ存入 − Σ已领取 − Σ已退款（不变量测试逐步验证）。
- **参数校验**：拒绝零时长（`end == start` → `ZeroDuration`）、倒置时间
  （`end < start` → `InvertedTimes`）、非法 cliff（`InvalidCliff`）、零金额、零地址。
- **重入防护**：所有外部入口（create/topUp/claim/cancel）均带 `nonReentrant`，
  并遵循 CEI（先更新状态再转账）；测试用带转账回调的代币模拟 ERC777 风格重入。

## 目录结构

```
src/VestingStream.sol              # 归属合约
test/VestingStream.t.sol           # 单元测试（25 个：权限/时间校验/线性归属/余量/守恒/重入）
test/VestingHandler.sol            # 状态机 Handler：随机操作 + 独立模型 + 事件逐步核对
test/VestingStream.invariant.t.sol # 不变量测试（6 条不变量）
test/mocks/                        # MockERC20 / CallbackToken(回调) / ReentrantClaimer(攻击者)
script/VestingDemo.s.sol           # Anvil 演示脚本
examples/demo-inputs.json          # 演示脚本示例输入（环境变量同名覆盖）
foundry.lock                       # 依赖锁定（tag + commit）
```

## 不变量（随机序列逐步核对）

Handler 在每一步操作上维护独立模型与幽灵变量，并用 `vm.recordLogs`
核对合约实际发出的事件字段（金额、退款额、保留额、新总额）：

1. `invariant_conservation` — 合约余额 == 累计存入 − 累计领取 − 累计退款；
2. `invariant_claimableMatchesModel` — 每条流 `claimable` 与独立模型一致；
3. `invariant_stateMatchesModel` — 合约存储的每条流全部字段与模型一致；
4. `invariant_actorBalances` — 每个 actor 的代币余额 == 增发 − 流出 + 流入；
5. `invariant_claimedNeverExceedsVested` — 已领取 ≤ 已归属，领取+退款 ≤ 存入。

覆盖的随机操作：`createStream / topUp / claim / cancel / warp`、
**同一时间戳重复调用**（`claimTwiceSameTimestamp` / `cancelTwiceSameTimestamp`，
第二次必须 revert）、越权调用（非受益人 claim、非 sender cancel/topUp 必须 revert）。

## 本地启动

```bash
# 安装 Foundry（已安装可跳过）
curl -L https://foundry.paradigm.xyz | bash && foundryup

# 拉取锁定依赖（git submodule，版本见 foundry.lock）
git submodule update --init --recursive

# 编译
forge build
```

## 验收命令

```bash
# 全部测试：25 个单元测试 + 6 条不变量（64 runs × depth 64）
forge test

# 只看不变量测试（随机操作序列 vs 独立模型）
forge test --match-contract VestingStreamInvariantTest -vvv

# 只看重入与事件相关单元测试
forge test --match-test "Reentrancy" -vvv
```

## 本地链上演示（Anvil）

```bash
anvil --port 8546 &          # 启动本地链
forge script script/VestingDemo.s.sol --rpc-url http://127.0.0.1:8546 --broadcast
```

随后可用 `cast` 交互（地址见脚本输出，示例输入见 `examples/demo-inputs.json`）：

```bash
RPC=http://127.0.0.1:8546
cast rpc evm_increaseTime 7200 --rpc-url $RPC && cast rpc evm_mine --rpc-url $RPC
cast call $VESTING "claimable(uint256)" 0 --rpc-url $RPC          # 查询可领取额
cast send $VESTING "claim(uint256)" 0 --private-key $BEN_KEY --rpc-url $RPC   # 受益人领取
cast send $VESTING "cancel(uint256)" 0 --private-key $SENDER_KEY --rpc-url $RPC # 发送者撤销
```

## 依赖（已锁定）

| 依赖 | 版本 | commit |
|---|---|---|
| forge-std | v1.16.2 | `bf647bd6` |
| openzeppelin-contracts | v5.4.0 | `c64a1edb` |

锁定信息同时记录在 `foundry.lock` 与 git submodule 指针中。
