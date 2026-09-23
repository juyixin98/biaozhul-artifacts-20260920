# 恒定乘积池（Constant-Product AMM）核算

使用 **Solidity 0.8.26 + Foundry** 从零实现的双代币恒定乘积（x·y=k）流动性池，
纯后端、无前端。代币均为本地测试资产。包含一个**独立的高精度参考模型**（Python
任意精度 Decimal），通过状态序列测试与合约逐状态对账。

---

## 1. 快速开始（本地启动）

```bash
# 工具链：Foundry 1.8.3（forge/cast/anvil）
curl -L https://foundry.paradigm.xyz | bash
foundryup --version 1.8.3

# 构建
forge build

# 全量测试（单元 + 安全 + 边界 + 状态不变量 + 参考模型序列回放）
forge test -vv

# 端到端：自动启动一条临时 Anvil 链，部署→注入流动性→交换→加/撤流动性→逐步对账
bash script/replay.sh
```

Python 参考模型仅用标准库（Python ≥ 3.10，无需 pip）。

### 验收命令（一键全部跑通）

```bash
make test           # 51 个 Foundry 测试全绿
make vectors        # 用 Python 参考模型重新生成状态向量（确定性种子）
make replay         # 本地 Anvil 端到端可重放样例
make verify-deps    # （可选，需联网）校验 vendored 依赖与锁定提交逐字节一致
```

---

## 2. 目录结构

```
src/
  ConstantProductPool.sol     # 池合约：流动性、交换、份额、严格转账记账、重入锁、sync
  BalanceProbe.sol            # 主动探测"接收端扣费"代币的测量合约
  lib/CPMMMath.sol            # 纯整数数学（floor sqrt、交换输出、份额铸造/销毁）
  interfaces/IERC20.sol       # 最小 ERC-20 接口
  test/                       # 仅测试用资产/攻击合约（非生产代币）
    TestERC20.sol             #   诚实 ERC-20（可自由 mint）
    NoReturnToken.sol         #   不返回 bool 的 ERC-20（USDT 风格），池必须兼容
    FeeOnTransferToken.sol    #   转入即扣费代币（发送端扣费）
    OutgoingFeeToken.sol      #   从池转出时才扣费代币（接收端扣费）
    CallbackToken.sol         #   每次转账触发回调的代币（ERC-777 风格）
    ReentrancyAttacker.sol    #   回调中尝试重入所有入口的攻击者 + CREATE 工厂
reference/
  cpmm_ref.py                 # 独立参考模型：整数(EVM语义) + 80位连续高精度 双实现
  generate_vectors.py         # 确定性生成状态序列向量 + 守恒/精度/手续费检查
test/
  PoolUnit.t.sol              # 30 个精确取值单元/属性测试
  PoolSecurity.t.sol          # 11 个对抗测试（扣费代币/重入/原子回滚/捐赠）
  PoolEdge.t.sol              # 7 个边界测试（view、uint112 溢出、无限授权）
  PoolInvariant.t.sol         # 状态不变量模糊测试（swap/add/remove/转账份额）
  ReferenceVectors.t.sol      # 回放 Python 生成的 44 条操作序列，逐步逐状态对账
  vectors/vectors.json        # 生成的状态序列（44 个用例）
  vectors/manifest.json       # 向量 sha256、精度检查计数、手续费扫描
script/
  Deploy.s.sol                # Foundry 部署脚本（两个测试代币 + 池）
  replay.sh                   # Anvil 端到端可重放样例（含逐步断言）
  verify_deps.sh              # 依赖锁定校验
examples/
  sample-inputs.json          # 手工可读的示例输入与预期输出
  sample-sequence.json        # 参考模型生成的完整生命周期
  precision-report.txt        # 整数 vs 80 位连续模型的舍入差报告
DEPS.md                       # 依赖与版本锁定说明
```

---

## 3. 核算规则（明确的整数舍入语义）

所有除法都是 **EVM 地板除（floor）**，舍入方向始终**对池/存量 LP 有利**。

### 3.1 手续费：0.30%，从输入侧收取

```
b      = floor(amountIn · 997 / 1000)          # 先对手续费向下取整
out    = floor(b · reserveOut / (reserveIn + b)) # 再对输出向下取整
reserveIn'  = reserveIn  + amountIn
reserveOut' = reserveOut - out
```

- 双重 floor 保证实际输出**绝不超过**连续公式值；
- 每笔真实成交后 `k' = reserveIn'·reserveOut' > k`（手续费使 k 严格增长，
  单元测试与不变量测试都断言）；
- **极小金额不能绕过手续费**：当 `b == 0` 或 `out == 0` 时交换直接 `ZeroOutput`
  回滚。例如储备为 1000/1000 时输入 1～2 wei 都得到 0 输出。参考模型对
  10⁶～10³⁰ 的储备规模、1 wei 到整池储备的输入做了扫描，结构性断言
  「凡正输出则严格收取了手续费；零输出在链上被拒绝」。

### 3.2 最小流动性锁定

首次注入：`raw = sqrt(amount0·amount1)`（floor），其中 **1000 份额永久铸造到
`address(0)` 锁死**，LP 实际得到 `raw − 1000`。`raw ≤ 1000` 时回滚
`NoLiquidityMinted`，不铸造任何份额。锁仓使首笔注入无法被后续捐赠/操纵"抽干"，
且最后一份 LP 也永远无法赎回全部储备（残留 1000 份额对应的尘）。

### 3.3 添加流动性（后续）

池按当前比例**自动计算实际收取量，任一侧都不会超过用户 desired**：

```
opt1 = floor(a0Desired · r1 / r0)
若 opt1 ≤ a1Desired: 取 (a0Desired, opt1)
否则:                取 (floor(a1Desired · r0 / r1), a1Desired)
shares = min(floor(a0·S/r0), floor(a1·S/r1))     # 两侧 floor，取小
```

`shares == 0` 回滚 `InsufficientShares`；低于 `minShares` 回滚
`SlippageShares`。滑点检查在**任何状态变更之前**完成（与 EVM 原子性一致，
参考模型也采用"先计算校验、后提交"两阶段）。

### 3.4 移除流动性

```
amount0 = floor(shares · reserve0 / totalShares)
amount1 = floor(shares · reserve1 / totalShares)
```

两侧都 floor（尘留在池里），`amount0/amount1 == 0` 回滚 `ZeroOutput`，
低于各自 `minAmount` 回滚 `SlippageToken`。

### 3.5 交换的滑点与截止时间

- `minAmountOut`：计算输出 < 下限 → `MinOutputNotMet(实际, 下限)`；
- `deadline`：`block.timestamp > deadline` → `Expired`（add/swap/remove 三个入口都有）。

### 3.6 LP 份额

池自身实现一份最小 ERC-20（`CPMM LP / CPLP`，18 decimals）：`transfer`、
`approve`、`transferFrom`（支持 `type(uint256).max` 无限授权且不递减）、
`balanceOf`、`totalSupply`。份额只可能由注入铸造、由撤出销毁。

---

## 4. 安全设计

| 威胁 | 处理 |
|---|---|
| **转账扣费（fee-on-transfer）代币** | 两层防御。① 入金：`_takeIn` 用转账前后余额严格断言池余额**恰好增加名义额**，发送端扣费→`UnexpectedBalanceDetected`。② 出金：`_pushOut` 同时断言池余额恰好减少、**接收方余额恰好增加名义额**（读的是代币自身 `balanceOf`，接收合约无法伪造），接收端扣费→`FeeOnTransferDetected`；首存还额外用 `BalanceProbe` 主动探测"池作为发送方时扣费"的代币。扣费可在首存**之后**才开启，实时出金检查仍会拒绝。 |
| **回调重入** | 单一存储槽互斥锁（1 解锁/2 锁定）包裹 add/remove/swap/sync。ERC-777 风格的代币在输入转入与输出转出两个时点回调，攻击者在回调中重入 swap/add/remove，均以 `ReentrancyLocked` 失败，外部调用正常完成。销毁份额遵循"先改状态后外部转账"。 |
| **失败原子回滚** | 所有校验先于状态变更；任何一步 revert 由 EVM 撤销整笔交易（转账、授权、份额、储备全部回到原状）。`PoolSecurityTest` 逐笔断言失败后余额/份额/储备不变。 |
| **直接转账（捐赠）** | 池每笔操作结尾都断言 `储备 == 真实余额`。裸转代币会使二者失配，后续操作回滚 `ReservesDiverged`；`sync()` 可把捐赠并入储备但**不铸造任何份额**（按比例归属全体存量 LP）。首存前池必须为空（构造器 + 运行时双重 `TokenPreFunded` 防护）。 |
| **非标准 ERC-20** | 底层 call 同时兼容"不返回数据"（USDT 风格）与"返回 true"，其它返回值/调用失败→`TransferFailed`。 |
| **uint112 溢出** | 储备以 uint112 存储，提交前越界回滚而非截断。 |
| **非法收款方/交易对** | `tokenIn` 非两代币→`InvalidTokenIn`；`to` 为 0/池自身/任一代币合约→`ZeroRecipient`；构造拒绝相同代币与零地址。 |

核心不变量（每个状态变更后强制成立）：

```
token0.balanceOf(pool) == reserve0
token1.balanceOf(pool) == reserve1
```

---

## 5. 独立高精度参考模型与测试方法

`reference/cpmm_ref.py` 中同时存在**两份相互独立**的经济描述：

1. **整数模型**：逐 floor 复刻 EVM 语义（sqrt 用 Python `math.isqrt`，与
   Solidity 的巴比伦迭代是不同实现，靠测试证明一致）；
2. **连续模型**：含手续费的 x·y=k 公式用 80 位有效数字 `Decimal` 计算，
   **没有任何舍入**，作为真值。

每个整数结果都与连续值比较并断言舍入差落在**解析上界内**（交换有两层
floor，上界按每笔交易储备比推导：`1 + R·r/(r+b)²`；铸造/销毁 `<1`）。
`examples/precision-report.txt` 汇总最大舍入差与手续费扫描。

`generate_vectors.py`（种子 `0x1337`，完全确定可复现）生成：

- 14 个**手写边界用例**（锁定、尘埃、过期、滑点、非法代币、超烧、欠额、
  原子加撤往返……）+ 30 个**随机生命周期**（斜向初始比例、多 LP、随机
  swap/add/remove 交织）；
- 每一步都带：输入、时间戳、成功/回滚选择器、返回值、**操作后完整状态快照**；
- 生成器自身断言：代币守恒（Σ账户余额+储备=初始发行量）、份额守恒
  （Σ余额=总供应）、1000 锁仓恒定、每笔 swap 后 k 不降。

`ReferenceVectors.t.sol` 把这 44 条序列在 EVM 上逐步重放，断言**每一步后
储备、总份额、每个账户的两种余额与 LP 份额与模型完全一致**，回滚步骤则断言
链上状态与模型一样"无痕迹"。它还通过 SHA-256 预编译（address 0x02）校验
vectors.json 的哈希与 manifest 一致，防止向量被悄悄改动。

此外：

- `PoolInvariantTest` 用 Foundry invariant 引擎随机调用
  swap/add/remove/份额转账（数千次），持续断言：储备==余额、份额供应守恒、
  锁仓恒为 1000、无幽灵持有人、份额始终有双币支撑、k 单调；
- `PoolUnit.t.sol` 含两个 `testFuzz_` 属性测试（交换公式/k 增长；加后即撤
  不稀释存量 LP、单位份额赎回价值不下降）。

---

## 6. 可重放操作样例

```bash
bash script/replay.sh
```

脚本在一条全新 Anvil 上执行并断言：

1. 部署 TKN0/TKN1/池；
2. alice `addLiquidity(1000,1000)` → 份额 `999999999999999999000`，锁 `1000`；
3. bob `swapExactInput(10 TKN0, minOut=0)` → 精确输出
   `9871580343970612988`（由 Python 模型现场计算比对），k 严格增长；
4. 输入 1 wei → `ZeroOutput`；过期交易 → `Expired`；
5. bob 先 add 再全部 remove → 份额归零，比例保持（floor 尘留池）；
6. 结尾断言池的两种代币**实际余额 == 储备**。

完整机器可读记录写入 `examples/replay-output.txt`；手工示例见
`examples/sample-inputs.json` 与 `examples/sample-sequence.json`。

手动用 cast 操作（`KEEP=1 bash script/replay.sh` 可保留链）：

```bash
cast send $POOL "addLiquidity(uint256,uint256,uint256,address,uint256)" \
  1000000000000000000000 1000000000000000000000 0 $ALICE <deadline> \
  --rpc-url http://127.0.0.1:8545 --private-key $ALICE_KEY
cast call $POOL "getReserves()(uint112,uint112,uint256)" --rpc-url http://127.0.0.1:8545
```

---

## 7. 重新生成向量

```bash
make vectors      # 或 python3 reference/generate_vectors.py
forge test -vv    # 合约侧回放会自动读取新向量
```

生成是确定性的（固定 RNG 种子）；manifest 记录向量 sha256，合约测试用
SHA-256 预编译复核该哈希。

---

## 8. 依赖锁定

见 [DEPS.md](DEPS.md)。要点：唯一依赖 forge-std 已 **vendored** 到 `lib/`
（构建/测试完全离线），与上游提交 `7239323e35487ba4339c93fe591065a63ce122aa`
逐字节一致，`bash script/verify_deps.sh` 可复核；工具链锁定 foundry 1.8.3、
solc 0.8.26。

---

## 9. 测试结果

```
5 个测试套件，51 个测试全部通过：
  PoolUnitTest        30  精确取值 + fuzz 属性
  PoolSecurityTest    11  扣费代币(双向) / 重入 / 原子性 / 捐赠 sync
  PoolEdgeTest         7  view / uint112 溢出 / 无限授权 / 大整数 sqrt
  ReferenceVectorsTest 2  44 条参考模型序列逐步对账 + 向量哈希
  PoolInvariantTest    1  数千次随机操作下的 5 项状态不变量

核心合约行覆盖率：ConstantProductPool 95%+，CPMMMath / BalanceProbe 100%。
```

所有计算（整数 AMM 公式、floor sqrt、SHA-256 哈希校验）、协议交互
（Anvil 部署/交易/回调）均为真实执行；失败的命令会如实报告并导致对应步骤
非零退出。
