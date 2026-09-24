# Share Vault — 份额金库舍入不变量

一个 ERC-4626 风格的份额金库：**Solidity + Foundry** 合约与测试，**FastAPI + web3.py**
后端 HTTP 接口，链环境只用本机 **Anvil** 与测试密钥，资产仅用模拟 ERC20（`MockERC20`）。
全部换算使用整数算术（512 位中间值 `mulDivDown`），无浮点。

## 设计要点

### 舍入方向（整数算术，永远向下取整、偏向资金池）

| 操作 | 公式 | 方向 |
|---|---|---|
| deposit | `shares = floor(assets * (S + 1000) / (A + 1))` | 份额向下 |
| redeem  | `assets = floor(shares * (A + 1) / (S + 1000))` | 资产向下 |

`S` = 实际份额总量，`A` = 金库持有资产。赎回向下取整会把不足 1 单位的零头留在池内，
因此**份额价格单调不减**（不变量测试 `invariant_rateNonDecreasing` 验证）。

### 空库初始化

无引导存款、无锁定流动性。用 **虚拟偏移**（OpenZeppelin ERC4626 同款模型）：
1000 虚拟份额 + 1 虚拟资产，使空库汇率也有定义——空库存入 1 wei 铸造 1000 份额，
全额赎回精确拿回 1 wei（`test_tinyDeposit_oneWei_roundTrips`）。

### 捐赠攻击（首存膨胀攻击）处理

`VIRTUAL_SHARE_OFFSET = 1000`：攻击者想让受害者存款舍入归零，必须捐赠约 1000 倍
的资产，而赎回时最多拿回约 1/1001——**每可能获利 1 单位需先损失约 1000 单位**。
直接向金库转账（捐赠）只会按比例抬高所有持有者的价格，永远无法铸成份额。
已知残余风险（见合约 NatSpec）：向**完全空仓**（无任何存款）的捐赠会被首位存款人
的 1:1000 引导汇率吸收，属于捐赠者对首存人的赠与，不会损害任何既有持有者。

### 最大滑点

`deposit(assets, receiver, minSharesOut)` / `redeem(shares, receiver, minAssetsOut)`：
报价与成交之间若被捐赠/MEV 移动价格，成交差于调用者下限即以 `Slippage` 回滚
（`test_slippage_donationBetweenQuoteAndFill_reverts`）。API 层支持
`min_shares_out` / `min_assets_out` 或 `max_slippage_bps`（由 preview 推导下限）。

### 其他

- **重入防护**：`nonReentrant` 锁 + 先更新状态后外部调用；恶意 ERC20 回调测试
  （`ReentrantERC20`）验证存款/赎回两条路径的重入均被拒绝。
- **收费代币兼容**：存款按余额差值入账（`test_feeOnTransfer_creditsReceivedAmount`）。
- **Math512**：`floor(x*y/d)` 使用 512 位中间乘积，溢出/除零显式回滚。

## 目录结构

```
src/ShareVault.sol        金库合约（份额 ERC20 + 存取 + 滑点 + 重入锁）
src/Math512.sol           512 位中间值整数乘除
src/MockERC20.sol         模拟资产（18 位小数，公开 mint，仅限本地测试）
src/SafeTransferLib.sol   安全转账（兼容无返回值代币）
src/interfaces/IERC20.sol
test/ShareVault.t.sol           单元 + 模糊测试（含捐赠攻击、重入、滑点、极小存款、全额赎回）
test/ShareVault.invariant.t.sol 不变量模糊测试（资产守恒/价格单调/份额足额/供应一致）
test/mocks/                     恶意回调代币、转账收费代币
backend/app/              FastAPI 应用（config/chain/schemas/main）
backend/app/abi/          由 forge 产物导出的 ABI + bytecode
backend/tests/            pytest 端到端 API 测试（自动拉起 Anvil 并部署）
scripts/deploy_local.py   部署到本地 Anvil，写 deployment.local.json
scripts/export_abis.py    从 out/ 导出 ABI 到 backend/app/abi/
scripts/demo.sh           一键端到端演示
requirements.txt          锁定的 Python 依赖
foundry.toml              锁定的 solc 0.8.26 / fuzz 参数
```

## 依赖与启动

依赖：Foundry（forge/anvil，本机验证版本 1.8.3）、Python 3.12、forge-std v1.9.7
（`lib/forge-std`，pin 在 `77041d2`）。

```bash
# 1) 合约依赖（如 lib/forge-std 缺失）
forge install foundry-rs/forge-std@v1.9.7 --no-commit

# 2) Python 环境
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# 3) 合约测试（单元 + 模糊 + 不变量）
forge test

# 4) 后端 API 测试（自动启动 Anvil、部署、跑 HTTP 用例）
python -m pytest backend/tests -q

# 5) 一键端到端演示（Anvil → 部署 → API → 示例调用）
./scripts/demo.sh
```

手动分步启动：

```bash
anvil --port 8545 &                       # 本地链（测试密钥，勿用于真实网络）
forge build && python scripts/export_abis.py
python scripts/deploy_local.py            # 写 deployment.local.json
uvicorn app.main:app --app-dir backend --port 8000
```

环境变量：`RPC_URL`（默认 `http://127.0.0.1:8545`）、`PRIVATE_KEY`
（默认 Anvil 账户 #0 的公开测试密钥，**仅限本地**）、`DEPLOYMENT_FILE`。

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 链连接状态、chain_id、服务器账户 |
| GET | `/vault` | 总资产/总份额/汇率/舍入规则说明 |
| GET | `/vault/preview/deposit?assets=` | 存款报价（份额） |
| GET | `/vault/preview/redeem?shares=` | 赎回报价（资产） |
| POST | `/vault/deposit` | `{assets, min_shares_out? \| max_slippage_bps?}` |
| POST | `/vault/redeem` | `{shares, min_assets_out? \| max_slippage_bps?}` |
| POST | `/faucet` | 铸造模拟代币 `{to?, amount}` |
| GET | `/account/{address}` | 资产余额/份额余额/份额估值 |

金额均为 wei 整数字符串。服务器以本地测试账户作为存取款调用方。

## 测试结果（2026-09-24 实际运行，如实记录）

- `forge test`：**22 passed / 0 failed**
  - ShareVaultTest：17 项（5 个 fuzz 各 2000 runs：往返不牟利、捐赠攻击永不获利、
    全额赎回仅剩粉尘、舍入方向、次存款人舍入偏向池；12 个单元：1 wei 极小存款往返、
    捐赠攻击具体场景、重入回调×2、滑点×3、收费代币、价格单调、零值回滚等）
  - ShareVaultInvariantTest：4 条不变量（256 runs × depth 30，7680 次调用，0 异常回滚）：
    资产守恒、汇率单调不减、份额足额兑付、供应一致
  - Math512Test：4 项（含 2000 runs 与朴素乘除对照、溢出/除零回滚）
- `python -m pytest backend/tests -q`：**9 passed**（自动起 Anvil 部署后跑 HTTP 全流程：
  1 wei 存款铸 1000 份额并精确赎回、报价与成交一致、滑点回滚、捐赠使持有者受益等）
- `./scripts/demo.sh`：端到端跑通——1 wei 存款 → 1000 份额；再存 3 代币；
  全额赎回精确拿回 3000000000000000001 wei，金库清零。

## 未完成项 / 已知限制

- 金库未实现 ERC4626 的 `mint`/`withdraw`（按份额存、按资产取）入口，仅
  `deposit`/`redeem`；`maxDeposit/maxRedeem` 等视图未暴露。
- 份额赎回仅限本人（无 allowance 赎回路径）。
- 空库前若有捐赠，首位存款人按 1:1000 引导价吸收该捐赠（文档化的设计取舍）。
- 后端为单账户服务模式（服务器代持一个本地测试密钥），无多用户/鉴权；
  仅面向本地 Anvil，不可直接上主网。
- `MockERC20` 任何人可 mint——仅限本地测试。
