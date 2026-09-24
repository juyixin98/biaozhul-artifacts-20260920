# 无损精度费率结算（Lossless Precision Fee Settlement）

Solidity + Foundry 合约、Python + FastAPI + web3.py HTTP 服务，在本机 Anvil 链上运行。
核心性质：**小额费用永不因整数截断而永久丢失** —— 每次结算的舍入余数（remainder）结转到下一次结算。

## 原理

每账户每区块费用（整数公式，`RATE_SCALE = 1e18`，费率为尾数，`1e18` = 100%/区块）：

```
numerator = principal * ratePerBlock * nBlocks + carryIn
fee       = numerator / 1e18        （向下取整，计入应计费用）
carryOut  = numerator mod 1e18      （余数结转，永久保存在链上状态）
```

- `principal * ratePerBlock` 可超过 2^256，因此使用 OpenZeppelin `Math.mulDiv`
  （512 位中间精度）计算商、`mulmod` 计算余数。
- 整数恒等式保证：**跨 100 块一次结算 == 逐块结算 100 次**（费用与余数都完全相等）。
- 舍入误差界：已结算费用始终 ≤ 精确实数费用，且差额严格小于 1 个基本单位；
  差额以余数形式留在状态里，不丢失。
- 费率变更（`setRate`）与本金变更（`setPrincipal`）都**先结算再变更**，旧费率下
  累计的费用不会被重新定价。
- 同一区块内重复 `settle` 返回 0（`n = 0` 为合法无操作），费用不会重复计入。
- 极端输入（如 max 本金 × 100% 费率 × 多块）使正确结果超过 2^256 时，
  交易**明确 revert**（checked arithmetic），不会静默回绕出错。

## 目录结构

```
src/LosslessFeeSettlement.sol   合约（唯一业务逻辑）
lib/openzeppelin/.../Math.sol   vendored OpenZeppelin v4.9.6（SHA256 见下）
lib/forge-std/                  forge-std v1.9.7（commit 77041d2c）
test/LosslessFeeSettlement.t.sol  Foundry 测试（15 个，含 2 个 fuzz）
app/main.py                     FastAPI + web3.py HTTP 服务
tests/test_e2e.py               端到端测试（真实 Anvil + HTTP，7 个）
scripts/demo.py                 现场演示脚本
foundry.toml                    锁定的编译配置（solc 0.8.24）
requirements.txt                pip freeze 锁定的 Python 依赖
```

## 依赖

| 组件 | 版本 | 说明 |
|---|---|---|
| Foundry (forge/anvil/cast) | 1.8.3 | 位于 `~/.foundry/bin` |
| solc | 0.8.24 | foundry.toml 锁定 |
| OpenZeppelin `utils/math/Math.sol` | v4.9.6 | vendored，SHA256 `85a2caf3…a52345` |
| forge-std | v1.9.7 (`77041d2c`) | lib/forge-std |
| Python | 3.12 | venv |
| fastapi / web3 / uvicorn / pytest / requests / httpx | 见 requirements.txt | pip freeze 全量锁定 |

## 启动与测试

```bash
export PATH=$HOME/.foundry/bin:$PATH

# 1. 合约编译 + 测试
forge build
forge test -v

# 2. Python 环境（首次）
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 3. 端到端测试（自动拉起独立 Anvil + uvicorn，无需手动启动）
.venv/bin/python -m pytest tests/ -q

# 4. 现场演示（自动拉起 Anvil + 服务，对比批量 vs 逐块结算）
.venv/bin/python scripts/demo.py

# 5. 手动运行服务（可选）
anvil --port 8545 &                       # 终端 1
.venv/bin/uvicorn app.main:app --port 8000 # 终端 2（自动部署合约）
curl localhost:8000/health
```

环境变量：`RPC_URL`（默认 `http://127.0.0.1:8545`）、`PRIVATE_KEY`
（默认 Anvil 第一个测试密钥，**仅限本地**）、`CONTRACT_ADDRESS`
（不设置则启动时自动部署）。

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 链连接、合约地址、当前块高 |
| POST | `/accounts` | 开户 `{address, principal, ratePerBlock}` |
| GET | `/accounts/{address}` | 查询状态（含待结算块数、预计费用、余数） |
| POST | `/settle` | 结算 `{address}` |
| POST | `/rate` | 改费率（先结算）`{address, ratePerBlock}` |
| POST | `/principal` | 改本金（先结算）`{address, principal}` |
| POST | `/claim` | 提取已结算费用 `{address}` |
| POST | `/close` | 结算并冻结账户 `{address}` |
| GET | `/quote` | 纯函数报价 `?principal=&ratePerBlock=&blocks=&carry=` |
| POST | `/admin/mine` | 本地挖 n 块（Anvil 专用测试辅助） |

注意：演示服务用单一运营密钥签名所有交易，合约账户以 `msg.sender` 为键，
因此 `address` 必须等于 `/health` 返回的 `operator`。多用户部署需改为每用户
密钥中继或 meta-transaction。

## 验收标准与实际运行结果

| 验收项 | 测试 | 结果 |
|---|---|---|
| 跨 100 块一次结算 == 逐块结算 | `test_100Blocks_BatchEqualsStepwise`（Foundry）、`test_100_blocks_batch_equals_stepwise`（e2e）、`testFuzz_BatchEqualsStepwise`（256 组随机） | ✅ 费用与余数完全相等 |
| 零本金 | `test_ZeroPrincipal` / `test_zero_principal` | ✅ 费用与余数恒为 0，claim 正确 revert |
| 极小费率（1e-18/块） | `test_MinimalRate` / `test_minimal_rate_dust_promotes` | ✅ 999 块纯结转，第 1000 块 dust 提升为 1 单位 |
| 最大值 | `test_MaxPrincipal_MulDiv512`（P=2^256−1，P·R 超 256 位走 512 位路径）、`test_MaxRate_ExactAccumulation`、`test_Overflow_RevertsNotWraps` | ✅ 结果与 Python 大整数参考完全一致；真溢出时 revert 而非回绕 |
| 舍入误差界 | `test_rounding_error_bound`、`test_DustIsNeverLost` | ✅ `settled*1e18 + remainder == exact`，误差 < 1 基本单位 |
| 费用不重复计入 | `test_NoDoubleCounting_SameBlockSettle` / `test_no_double_counting`、`test_Claim_ThenNoDoubleSpend` | ✅ 同块重复结算得 0；claim 后余额归零 |
| 费率变更先结算 | `test_SetRate_SettlesOldRegimeFirst` / `test_rate_change_settles_old_regime` | ✅ 旧费率费用按旧率结清后才切换 |

实际运行记录（2026-09-24，本机）：

```
forge test        → 15 passed; 0 failed（含 2×256 组 fuzz）
pytest tests/ -q  → 7 passed（每个测试独立 Anvil + uvicorn 实例）
scripts/demo.py   → principal=1000003, rate=0.3%/块, 100 块:
                    批量 fee=300000 rem=0.9  ==  逐块 fee=300000 rem=0.9
                    fee*1e18 + rem == 精确费用 300000.9（无损）
```

## 已知限制 / 未完成项

- **单运营密钥**：HTTP 服务以单一密钥签名，账户键即 `msg.sender`，故一个服务
  实例只服务一个链上账户（第二个账户的演示在测试/脚本里用 web3.py 直接签名）。
  生产化需每用户密钥或账户抽象。
- **无 ERC20 转账**：费用是合约内记账余额（`claim` 只清零并记录事件），未接
  真实代币转账；接入时在 `claim` 中加 `IERC20(token).transfer` 即可，结算数学不变。
- **费率上限 100%/块**：`ratePerBlock > 1e18` 被拒绝；更高费率需调整公式中
  `per * n` 的溢出边界分析。
- **关闭后不可重开**：`close` 冻结账户，剩余不足 1 单位的余数永久留在存储中
  （可通过 `getAccount` 读取），这是设计取舍而非丢失。
- 未做：gas 优化（如打包多账户结算）、事件索引子图、CI 配置。
