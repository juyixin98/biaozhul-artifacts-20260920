# Lossless Fixed-Rate Fee Settlement

本地链上按区块累计的**无损精度固定费率结算**：整数指数 + 余数结转，小额费用（dust）永远不会被舍入丢弃；批量跨 N 块结算与逐块结算结果**数学恒等**。

技术栈：Solidity 0.8.24 + Foundry（合约与合约测试）、Python 3.12 + FastAPI + web3.py（HTTP 接口与端到端测试）、本地 Anvil 测试链。**不依赖任何外部链或密钥。**

---

## 1. 设计

### 费率模型（全程整数，无浮点）

每个账户在本金 `p`、每区块费率 `r` 下，跨 `n` 个区块结算：

```
T     = carry + n · p · r
fees += ⌊T / SCALE⌋
carry  = T mod SCALE          (SCALE = 2^64)
```

- `r / 2^64` 是每区块固定费率，`r ≤ 2^64`（单区块费用不超过本金）；
- 64 位 `carry` 保存不足 1 个最小单位的零头，下一次结算结转，**dust 永不丢失**；
- 单区块递推式 `carry' + SCALE·fees' = carry + p·r` 迭代 n 次后望远镜求和，
  直接得到上面的批量公式，因此「一次跨 100 块」与「逐块 100 次」逐位相等；
- 舍入误差界：`0 ≤ n·p·r − fees·SCALE < 2^64`，即以费率尺度表示严格小于 1 个费用单位，且该误差被保留在 carry 中。

### 为什么需要 512 位整数

`p` 是 256 位，`r ≤ 2^64`，块数最多约 `2^64`，乘积可达约 384 位（极端边界），EVM 的 256 位寄存器放不下——截断本身正是本项目要消灭的「丢精度」bug。

`contracts/U512.sol` 用 4 个 128 位肢体（base 2^128）在 uint256 寄存器内实现纯整数 512 位乘法 / 加法 / 除以 2^64（位移，无长除法），所有中间值都有上界论证（见注释），溢出时 revert。

### 结算时机（不重复计入）

- `settle` / `setRate` / `setPrincipal` 都**先结算再变更**：费率/本金变更不追溯既往区块；
- 同区块重复结算返回增量 0（`block.number ≤ lastSettleBlock` 直接 no-op），费用不会重复计入；
- 本金或费率为 0 的区间不产生费用，且 **carry 原样保留**（清零 carry 本身就是丢精度）。

---

## 2. 目录结构

```
contracts/
  U512.sol            512 位无符号整数库（128 位肢体）
  FeeSettlement.sol   结算合约：开户/结算/改费率/改本金
test/                 Foundry（Solidity）测试
scripts/
  deploy.py           向本地 Anvil 部署合约，写 deployment.json
  demo.py             100 块批量 vs 逐块端到端演示
app/
  config.py           环境变量配置（默认指向本地 Anvil 与测试密钥）
  reference.py        纯 Python 大整数参考模型（独立复核链上结果）
  chain.py            web3.py 合约加载/部署/交易封装
  main.py             FastAPI HTTP 接口
tests/                pytest 端到端测试（自动起 Anvil、编译、部署）
foundry.toml          Solc 0.8.24, via-IR, 本地测试放开区块 gas 上限
requirements.txt      直接依赖（版本钉死）
requirements-lock.txt 全部传递依赖锁定（pip freeze）
```

---

## 3. 依赖与安装

需要：`curl`、Python ≥ 3.10（实测 3.12）、Foundry（anvil/forge）。

### Foundry

```bash
curl -L https://foundry.paradigm.xyz | sh
foundryup          # 实测版本 forge/anvil 1.8.3 (solc 0.8.24)
export PATH="$HOME/.foundry/bin:$PATH"
```

### Python

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-lock.txt   # 或 requirements.txt
```

锁定的主要版本：web3 6.20.3、fastapi 0.115.6、uvicorn 0.32.1、pydantic 2.10.3、
pytest 8.3.4、httpx 0.28.1（完整传递依赖见 `requirements-lock.txt`，实测环境 56 个包）。

### 编译合约

```bash
forge build
```

---

## 4. 启动与使用（本地 Anvil）

### 4.1 启动测试链

```bash
anvil --port 8545 --gas-limit 300000000
```

（逐块结算测试在一笔测试交易内跨很多区块，本地链需要放开区块 gas 上限；
合约本身的批量结算为 O(1)，不受此限制。）

### 4.2 部署

```bash
.venv/bin/python scripts/deploy.py
# -> FeeSettlement deployed at 0x5FbDB2315678afecb367f032d93F642f64180aa3
# -> 写入 deployment.json
```

可用环境变量覆盖默认值：`RPC_URL`（默认 `http://127.0.0.1:8545`）、
`PRIVATE_KEY`（默认 Anvil 0 号测试私钥）、`CONTRACT_ADDRESS`、`DEPLOYMENT_FILE`。

### 4.3 启动 HTTP 服务

```bash
.venv/bin/uvicorn app.main:app --port 8077
```

接口（大整数均为十进制字符串）：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 链 ID、当前区块、合约地址、签名者 |
| GET | `/scale` | 费率尺度 `2^64`、最大费率、uint256 上限 |
| POST | `/accounts` | `{account, principal, rate}` 开户 |
| GET | `/accounts/{account}` | 状态：本金/费率/结算块/carry/费用（整数 + 4×128 位肢体） |
| POST | `/accounts/{account}/settle` | 结算到当前区块，返回本次费用增量 |
| POST | `/accounts/{account}/rate` | `{rate}` 先结算再改费率 |
| POST | `/accounts/{account}/principal` | `{principal}` 先结算再改本金 |
| POST | `/debug/mine` | `{blocks}` 本地链空块推进（evm_mine） |

错误约定：未知账户 404；费率/本金越界 422；合约 revert（重复开户、非账户所有者）400。

### 4.4 运行演示

```bash
.venv/bin/python scripts/demo.py        # 重复跑可加偏移参数： demo.py 1
```

---

## 5. 自动化测试

```bash
# Solidity：库差分测试 + 合约性质测试（300 runs fuzz）
forge test

# Python：自动编译、起 Anvil(18545)、部署并跑全部 HTTP 端到端用例
.venv/bin/python -m pytest tests/ -q
```

---

## 6. 实测结果（本机实际运行，如实记录）

环境：Linux 6.8、Python 3.12.3、forge/anvil 1.8.3（solc 0.8.24）。

### Foundry：25 个用例全部通过

- `U512.t.sol`（13 个）：已知向量（max256² 等）、对 EVM 256 位乘法的 fuzz 差分
  （300×4 组）、512 位溢出 revert（外部 harness 边界）；
- `FeeSettlement.t.sol`（12 个）：100 块批量 vs 逐块、随机 fuzz 恒等
  （`uint128 × uint64 × uint8`，300 组）、零本金、极小费率 dust 结转、
  max(uint256) 本金 + 100% 费率 × 100 块（费用超 256 位）、费率/本金变更前先结算、
  零费率区间保留 carry、同区块重复结算 no-op、权限与费率上限。

```
Ran 2 test suites in 101.16ms: 25 tests passed, 0 failed, 0 skipped
```

### pytest：14 个用例全部通过

```
14 passed, 1 warning in 18.87s
```

覆盖：100 块批量 vs 逐块 vs Python 参考模型三方逐位相等、零本金、
p=r=1 极小费率（100 块后费用 0、carry 恰好 100）、最大值（fees > 2^256，
等于 `100·(2^256−1)`）、舍入误差界、增量之和等于余额（不重复计入）、
改费率分段正确、404/422/400 错误路径。

### 100 块对比实跑输出（p = 10^24，r = 2^64/100）

```
batch   opened at block 626, settled through 726: fees=999999999999999999132638 carry=4833260864108756992
per-blk opened at block 727, settled through 827: fees=999999999999999999132638 carry=4833260864108756992
python  reference over 100 blocks:               fees=999999999999999999132638 carry=4833260864108756992

exact fee   = 18446744073709551600000000000000000000000000/18446744073709551616
            = 999999999999999999132638 + 4833260864108756992/18446744073709551616
settled fee = 999999999999999999132638  (误差 < 1 最小单位，保存在 carry)
```

边界用例实测：

- **零本金**：100 块后 fees=0、carry=0；之后设置本金，只有新区间计息；
- **极小费率** p=1、r=1：100 块 fees=0、`carry=100`（零头全额结转）；
- **最大值** p=2^256−1、r=2^64、n=100：fees = `100·(2^256−1)` =
  `11579208923731619542357098500868790785326998466564056403945758400791312963983500`，
  超出 256 位（非零高位肢体），carry=0，链上与 Python 大整数一致。

### 验收对照

| 验收项 | 状态 |
|---|---|
| 一次跨 100 块 vs 逐块结算结果相同 | ✅ forge + pytest + demo + HTTP 实跑 |
| 零本金 | ✅ 不产生费用；区间后恢复计息正确 |
| 极小费率（dust 不丢） | ✅ carry 精确结转 |
| 最大值（无 256 位截断） | ✅ U512，链上 == 大整数参考 |
| 舍入误差界 | ✅ `0 ≤ 误差 < 2^64`，且误差即 carry |
| 费用不重复计入 | ✅ 同区块 no-op；增量之和 == 余额 |
| 费率变更前先结算 | ✅ 分段参考模型逐位一致 |
| 源码 / 锁定依赖 / 自动化测试 / README | ✅ |

---

## 7. 已知限制与未完成项（如实列出）

- 合约只做**费用记账**，没有 ERC-20 转账/提取逻辑（结算更新的是账面 fees 与 carry）；
- 账户所有者即开户交易签名者，除 owner 校验外没有更复杂的权限模型；
- 安全模型只覆盖本地 Anvil：默认私钥是公开测试私钥，`/debug/mine` 为裸奔的调试接口，**切勿照搬到公网**；
- 在「rate ≤ 2^64、本金 < 2^256、块号 ≤ uint64」的前提下 n·p·r < 2^384，
  永远放得进 512 位——合约的 512 位溢出保护在合法输入下不可达（库自身的保护经 harness 测试）；
- 逐块结算的 Solidity 测试为单交易循环，仅在本地放开 gas 上限的链上可行；
  真实主网应每区块/每批分交易结算，批量接口本身 O(1) 无此问题；
- 未配置 CI 工作流；测试各自使用独立 Anvil（pytest 固定 18545 端口）。
