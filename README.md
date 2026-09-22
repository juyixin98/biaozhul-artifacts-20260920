# 流式归属合约 · 不变量测试（StreamVesting）

纯后端 Solidity + Foundry 项目：实现一个**流式归属（streaming vesting）合约**，并为其配备
单元测试、模糊测试与**随机操作序列 + 独立模型**的不变量测试。

## 功能与规则

- **线性归属**：资金在 `[start, end]` 区间按时间线性归属，`cliff` 之前可领取额为 0。
- **整数余量结清**：归属额按 `amount * elapsed / duration` 整数除法计算，除法余量（dust）
  在 `end` 时刻一次性结清 —— `vestedOf(end) == amount` 精确成立。
- **撤销（cancel）**：仅发送者可调用；只把**未归属部分**退回发送者，受益人保留
  **已归属未领取**额度，之后仍可正常 `withdraw`。
- **资金补足（topUp）**：仅发送者可向未结束、未撤销的流追加资金（时间表不变，速率提高）。
- **资产守恒**：任意时刻 `token.balanceOf(合约) == Σ(每条流 amount - withdrawn)`
  `== 总流入 - 总领取 - 总退款`。
- **权限分离**：`withdraw` 仅受益人、`cancel`/`topUp` 仅发送者；越权调用一律 revert。
- **参数校验**：拒绝零时长（`end == start`）、倒置时间（`end < start`）、
  `cliff < start`、`cliff > end`、零金额、零地址受益人、已结束的归属计划。
- **重入防护**：所有资金操作带 `nonReentrant` 锁 + checks-effects-interactions；
  测试用带转账回调的 ERC20 模拟 ERC777 风格回调重入，验证攻击被阻断且状态不受损。

## 状态机

```
                createStream
                     │
                     ▼
        ┌────────────────────────┐
        │  Active（未撤销）       │ ◀── topUp（sender，t < end）
        │  vested(t) 随时间增长   │
        └────────────────────────┘
          │                 │
 withdraw │(beneficiary)    │ cancel（sender）
 （可多次，同一时间重复调用     ▼
   第二次必 revert）      Cancelled（归属冻结）
          │                 │ vested 保留给受益人，
          ▼                 │ 未归属部分已退回 sender
   withdrawn 增加            │ withdraw 仍可调用
                            ▼
                     余额最终归零（守恒）
```

## 目录结构

```
foundry.toml                     # solc 0.8.26 锁定、fuzz/invariant 配置
src/
  StreamVesting.sol              # 归属合约（自包含 ReentrancyGuard + SafeTransfer）
  interfaces/IERC20.sol          # 最小 ERC20 接口
  mocks/MockERC20.sol            # 测试代币（可编程转账回调，模拟 ERC777 钩子）
  mocks/ReentrantActor.sol       # 重入攻击者（sender/beneficiary 两种角色）
test/
  StreamVesting.t.sol            # 26 项单元/模糊测试：状态机迁移、权限、事件、
                                 #   余量结清、同时间重复调用、回调重入
  StreamVesting.invariant.t.sol  # 不变量测试：Handler 随机操作序列 + 独立模型
script/ExampleFlow.s.sol         # 端到端示例（读取示例输入 JSON）
examples/sample-input.json       # 示例输入
lib/forge-std                    # forge-std v1.9.7（git submodule，commit 锁定）
```

## 不变量测试设计

`VestingHandler` 对合约执行随机操作序列
（`createStream / warp / withdraw / cancel / topUp / unauthorized`，含 **0 秒 warp 的同时间重复调用**），
每一步都用**独立模型**交叉核对：

- **余额**：幽灵账本（`deposited / withdrawn / refunded`）与合约实际代币余额逐操作核对；
- **事件**：`vm.recordLogs` 捕获每条 `StreamCreated / Withdrawn / StreamCancelled /
  StreamToppedUp`，逐字段比对模型预期值；
- **可领取额**：模型用 `rate/rem` 拆分路径计算归属（与合约的单表达式除法是不同的计算路径），
  逐流断言 `withdrawable`、`vestedOf`、`withdrawn` 一致；
- **越权**：陌生人调用 `withdraw/cancel` 必须 revert（用低级 call 捕获，不污染 fuzz 运行）。

不变量（`runs=128, depth=40, fail_on_revert=true`）：

1. `invariant_assetConservation` — 资产守恒（双口径：幽灵账本 & 逐流义务求和）；
2. `invariant_modelMatchesContract` — 合约视图与独立模型逐流一致；
3. `invariant_callSummary` — 输出覆盖统计（流数量、被拦截越权次数、同时间重复次数）。

## 本地启动

```bash
# 1. 安装 Foundry（已安装可跳过）
curl -L https://foundry.paradigm.xyz | bash && foundryup

# 2. 拉取锁定依赖（forge-std v1.9.7 @ 77041d2c）
git submodule update --init --recursive

# 3. 编译（自动下载 solc 0.8.26）
forge build
```

## 验收命令

```bash
# 全部测试（单元 + 模糊 + 不变量）
forge test

# 只看不变量测试（随机操作序列 vs 独立模型）
forge test --match-contract StreamVestingInvariantTest -vvv

# 只看单元/模糊测试
forge test --match-contract StreamVestingTest -vvv

# 端到端示例（建流 → 补足 → 领取 → 撤销 → 受益人领取保留额）
forge script script/ExampleFlow.s.sol -vvv
```

最近一次本地运行结果：`29 passed; 0 failed`（含 3 条不变量，128 runs × 40 depth，
5120 次随机调用，0 reverts / 0 discards）。

## 依赖锁定

| 依赖 | 版本 | 锁定方式 |
|---|---|---|
| Solidity | 0.8.26 | `foundry.toml` 的 `solc_version` |
| forge-std | v1.9.7 (`77041d2ce690e692d6e03cc812b57d1ddaa4d505`) | git submodule |
| Foundry 工具链 | forge 1.8.3（开发环境实测） | 见上文安装命令 |

## 关键设计说明

- **余量结清**：`t >= end` 时直接返回 `amount`，不做除法，保证 `vestedOf(end) == amount`，
  整数除法产生的 dust 在结束时全部释放给受益人。
- **撤销语义**：`cancel` 时把 `amount` 就地收缩为当前已归属额并置 `cancelled`，
  之后 `vestedOf` 恒等于该值 —— 受益人权益不受撤销影响，守恒式自然成立。
- **topUp 与守恒**：补足只增加 `amount`（时间表不变），守恒式 `Σ(amount - withdrawn)`
  自动覆盖补足流入的资金。
