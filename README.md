# 恒定乘积池 (Constant-Product AMM)

Solidity + Foundry 实现的双代币恒定乘积（x·y=k）流动性池，**纯后端**，代币仅为测试资产。
包含工厂、交易对、路由三个合约，独立的高精度 Python 参考模型，状态序列交叉验证、
单元 / 模糊 / 不变量测试，以及可在本地 Anvil 一键重放的操作样例。

---

## 1. 目录结构

```
src/
  CPMMPair.sol          核心交易对：储备记账、mint/burn/swap、重入锁、k 校验
  CPMMFactory.sol       部署并索引每个无序代币对的唯一 Pair
  CPMMRouter.sol        用户入口：滑点(amountOutMin/amountMin)、截止时间、多跳路径
  CPMMMath.sol          整数公式库（全部向下取整，EVM 语义）
  ERC20.sol             最小 ERC20（LP 份额本身也是 ERC20）
  mocks/
    TestERC20.sol             普通测试代币（可自由铸造）
    FeeOnTransferERC20.sol    转账扣费代币（用于验证被拒绝）
    ReentrantERC20.sol        转账时回调重入的恶意代币
    ReentrantCallee.sol       闪兑回调中重入 / 不还款的恶意 callee
test/                 39 个测试：序列、单元、模糊、扣费代币、重入、不变量
reference/
  cpmm.py             独立参考模型：EVM 整数语义 + 100 位 Decimal 实数模型
  scenarios.py        状态序列场景生成器（产出夹具 JSON）
  fixtures/scenarios.json   生成的确定性夹具（被 Solidity 测试回放）
script/Deploy.s.sol   链上部署脚本
scripts/demo.sh       本地 Anvil 端到端可重放样例
examples/example-inputs.json  带注释的示例输入
```

---

## 2. 核心设计

### 2.1 手续费
- 每笔 swap 固定 **30 bps（0.30%）**，手续费留在池内（不单独分给 LP 地址）。
- 输出公式（整数向下取整）：
  ```
  amountOut = floor( amountIn * 997 * reserveOut
                   / (reserveIn * 1000 + amountIn * 997) )
  ```
- swap 结束用 **k 不变量**复核（缩放 1000 倍避免精度损失）：
  ```
  (bal0*1000 - in0*3) * (bal1*1000 - in1*3) >= reserve0 * reserve1 * 1_000_000
  ```
  任何少付 / 不还款（含恶意闪兑回调）都会令其失败并整体回滚。

### 2.2 最小流动性锁定
- 首次注资铸造 `sqrt(a0·a1)` 份额，其中 **1_000 份永久锁定到 `address(0)`**，
  其余给注资者。`sqrt(a0·a1) <= 1000` 时直接回滚。
- 锁定份额不可被任何人转移或销毁，因此：
  - 份额永不凭空增加（捐赠 / `skim` / `sync` 都不铸币）；
  - 即使流通份额全部销毁，池中仍保留锁定份额对应的资产。

### 2.3 整数舍入
- 所有除法向下取整；二次注资取两侧比例的较小者，**绝不少收多铸**；
- 销毁按 `floor(shares·reserve/totalSupply)` 分别两侧取整。
- Python 参考模型并行维护「EVM 整数」与「100 位 Decimal 实数」两条轨迹，
  断言整数输出满足 `real - 1 < floorOut <= real`，即舍入损失严格小于 1 wei。

### 2.4 储备与实际余额一致
- 储备 `reserve0/reserve1`（uint112）是缓存值，任何状态变更后都被设置为
  **真实代币余额**。测试在每个动作后断言 `reserve == token.balanceOf(pair)`。
- 直接转入（捐赠）不会被计入，可用 `skim()` 取回或 `sync()` 显式吸收。

### 2.5 拒绝扣费代币（fee-on-transfer）
- `mint` / `swap` 要求调用方**显式声明**到账增量（`expected0/expected1`、
  `expectedInput0/1`）。合约用真实余额增量与之比较，任何短少即
  `TransferFailed` 回滚。
- 路由在用户→交易对的转账边界再做一次余额增量校验（`FeeOnTransferDetected`）。

### 2.6 重入与原子性
- `mint / burn / swap / skim / sync` 共用同一把 `unlocked` 锁，
  恶意代币在转账中、或闪兑回调中重入任一入口都会得到 `Locked()`。
- 所有失败（截止时间、滑点、零输出、k 不满足、扣费、重入）都 revert，
  EVM 保证整笔交易原子回滚，无部分生效。

### 2.7 swap 的安全参数
- 路由强制 `deadline`（`block.timestamp > deadline` 即 `Expired()`）；
- 强制 `amountOutMin`，报价不达标即 `Slippage()`；
- 输出接收方禁止是任一代币合约或零地址。

---

## 3. 环境与依赖

- [Foundry](https://book.getfoundry.sh)（forge / cast / anvil），本仓库在 **1.8.3** 上验证。
  若已装在 `~/.foundry/bin` 但不在 PATH：
  ```bash
  export PATH="$HOME/.foundry/bin:$PATH"
  ```
- Python ≥ 3.10（仅标准库，用于参考模型，无需 pip 安装）。
- 依赖锁定：`lib/forge-std` 固定在 **v1.9.7**（见下，可复现安装）。
- Solidity 固定 **0.8.26**，EVM `cancun`，开启 optimizer + via-IR（`foundry.toml`）。

安装 / 复现依赖：
```bash
forge install foundry-rs/forge-std@v1.9.7 --no-commit --no-git
```

---

## 4. 本地启动与验收命令

```bash
# 0) 编译
forge build

# 1) 重新生成参考模型夹具（可选；夹具已签入，改动参考模型后需重跑）
python3 reference/scenarios.py

# 2) 运行全部自动化测试（单元 + 模糊 + 不变量 + 参考序列交叉验证）
forge test -vvv

# 3) 只跑参考模型自检（不依赖链）
python3 reference/scenarios.py --check

# 4) 端到端可重放样例：自动启动本地 Anvil、部署、注资、交易、撤资
./scripts/demo.sh
#   或对已运行的节点：
anvil            # 终端 A
RPC=http://127.0.0.1:8545 ./scripts/demo.sh   # 终端 B

# 5) 手动起链并部署
anvil
forge script script/Deploy.s.sol:Deploy --rpc-url http://127.0.0.1:8545 \
  --broadcast --private-key 0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80
```

### 验收要点（demo 与测试均会真实执行并打印）
| 要求 | 在哪验证 |
| --- | --- |
| 30 bps 手续费、整数舍入 | `Pair.t.sol::test_Swap_ChargesExactly30Bps`、dust 场景、Python 实数对照 |
| 最小流动性永久锁定 | `test_FirstMint_LocksMinimumLiquidity`、`full_withdrawal` 场景 |
| swap 最小输出 + 截止时间 | `Router.t.sol`（HonorsAmountOutMin / RevertsAfterDeadline / Slippage） |
| 添加 / 移除流动性保持份额比例 | `test_AddRemoveLiquidity_MaintainsProportion`、序列 burn 步 |
| 拒绝扣费代币 | `FeeOnTransfer.t.sol`（首存 / 续存 / swap / 路由四个入口） |
| 回调与转账重入被阻止 | `Reentrancy.t.sol`（swap/mint/burn/skim/sync + 闪兑回调） |
| 失败原子回滚 | 所有 `expectRevert` 用例 + 多跳失败回滚用例 |
| 储备 == 真实余额 | 每个序列步、不变量 `invariant_ReservesEqualBalances` |
| 份额不凭空增加 | `test_SharesCannotBeMintedFromThinAir`、`invariant_SupplyEqualsHolderSum` |
| 极小金额不能绕过手续费 | `dust_fee` 场景：零输出回滚 + withFee 输出 <= 零手续费输出 |

---

## 5. 独立高精度参考模型如何工作

`reference/cpmm.py` 不调用任何合约代码，是一份**独立重新实现**：

- `evm_*`：与 Solidity 完全一致的整数公式（用于逐位比对链上结果）；
- `real_*`：`decimal.Decimal`（100 位精度）上的理想曲线（用于界定舍入误差）；
- `CPMMPoolModel`：按顺序执行 fund / deposit / swap / burn 的状态机，
  每步断言「储备==余额」「锁定份额恒为 1000」「份额总和==totalSupply」。

`scenarios.py` 生成 4 个确定性场景并写入 `reference/fixtures/scenarios.json`：
1. `bootstrap_and_trade`：引导、非对称注资、双向交易、部分 / 全部撤资；
2. `dust_fee`：1 wei 级输入，零输出回滚，手续费不可被舍入绕过；
3. `micro_bootstrap`：最小流动性边界、二次注资向下舍入到 0 份额回滚；
4. `full_withdrawal`：流通份额全销毁后锁定份额对应的资产仍在池内。

`test/Sequence.t.sol` 在 EVM 上**逐步骤回放**该 JSON，逐步断言储备、余额、
总份额、各用户持仓与参考模型**完全相等**；标记 `expectRevert` 的步骤必须失败，
且失败后状态不变（失败交易被 `skim` 清回，模拟链上回滚后的干净起点）。

---

## 6. 计算 / 协议 / 密码操作说明

- 所有金额、k 不变量、份额、价格均在 EVM 上由 Solidity **真实计算**，
  测试与 demo 对真实 Anvil 节点发交易并读取回执 / 状态，不存在模拟或桩。
- 项目不含任何密码学原语（无签名、哈希承诺、随机数）；LP 份额是标准 ERC20
  转账记账。地址排序（token0 < token1）是纯数值比较，非密码操作。
- 若某项检查失败，测试 / 脚本会以非零退出并打印真实回滚原因。

---

## 7. 已知边界 / 非目标
- 代币为**测试资产**：`TestERC20` 任何人可铸造，切勿用于真实价值。
- 未实现闪电贷手续费分成、价格预言机累积、协议费率开关、LP 元数据扩展。
- 路由仅支持精确输入（exact-in）多跳；未实现精确输出（exact-out）。
