# 份额金库舍入不变量（Share Vault Rounding Invariants）

用 **Solidity + Foundry + Python + FastAPI + web3.py** 实现的一个本地份额金库（ERC-4626 风格）：
存入模拟 ERC20 资产得到份额，销毁份额按比例赎回资产。项目的核心是**整数舍入会计的
可证明安全边界**——存取款舍入方向、空库初始化、首发捐赠攻击、最大滑点保护、恶意代币
重入回调，全部用 Foundry 模糊测试 / 状态不变量测试和真实 Anvil 集成测试验证。

> 链环境**仅使用本机 Anvil 与其公开测试密钥**，HTTP 后端只连接 `http://127.0.0.1:8545`。
> 资产只有一个任何人可领的模拟 ERC20（`MockERC20`）。**不要用于真实资产。**

---

## 1. 目录结构

```
.
├── src/
│   ├── MockERC20.sol            # 模拟资产（faucet 式 mint，无手续费）
│   └── ShareVault.sol           # 份额金库：deposit/redeem + 死份额 + 重入锁 + 滑点
├── test/
│   ├── ShareVault.t.sol         # 单元 + 有界模糊：极小存款/首捐/全额赎回/滑点
│   ├── ShareVault.invariant.t.sol # 状态不变量（Handler 随机存款/赎回/捐赠/转账）
│   ├── MaliciousERC20.sol       # 转账时回调的恶意 ERC20
│   ├── MaliciousReentrancy.t.sol# 存/赎路径的重入攻击测试
│   └── handlers/Handler.sol     # 状态模糊的受限动作集 + ghost 变量
├── script/Deploy.s.sol          # forge 部署脚本（另有 Python 部署，见下）
├── lib/forge-std                # vendored forge-std v1.9.4（commit 1eea5bae…）
├── backend/app/                 # FastAPI + web3.py 后端
│   ├── config.py                # 本机 RPC、Anvil 测试密钥、路径
│   ├── chain.py                 # 部署、连接、整数滑点计算、交易发送
│   ├── schemas.py               # 请求/响应模型（金额一律整数）
│   └── main.py                  # HTTP 路由
├── backend/tests/               # pytest：真实 Anvil 上的链上 + HTTP 集成测试
├── scripts/deploy.py            # web3.py 部署到本机 anvil
├── scripts/demo.py              # web3.py 端到端示例
├── requirements.txt             # 直接依赖（精确版本）
├── requirements-lock.txt        # 完整传递依赖锁定（79 包，pip freeze 生成）
└── foundry.toml
```

---

## 2. 依赖

- **Foundry**（forge / anvil / cast），本项目实测版本 `v1.8.3`，Solidity `0.8.26`。
  安装：`curl -L https://foundry.paradigm.xyz | bash && foundryup`
- **Python ≥ 3.10**（实测 3.12.3），关键包：`web3==7.6.0`、`fastapi==0.141.1`、
  `uvicorn==0.53.0`、`pydantic==2.13.5`、`pytest==9.1.1`（完整锁定见 `requirements-lock.txt`）。
- 仅需本机，无需任何外部服务或真实账户。

---

## 3. 快速开始

```bash
# 0) 安装 Python 依赖（建议在 venv 中；也可手动 python3 -m venv .venv）
make install

# 1) 编译合约（首次会自动下载 solc 0.8.26）
make build

# 2) 启动本机 Anvil（另开一个终端；默认 127.0.0.1:8545，链 ID 31337）
make anvil

# 3) 部署 MockERC20 + ShareVault（地址写入 deployments/local.json）
make deploy

# 4) 启动 HTTP 后端（127.0.0.1:8000，交互式文档 /docs）
make api

# 5) 另开终端运行端到端示例
make demo
```

不用 make 的等价命令见各 Makefile 目标。

---

## 4. 舍入规则与安全模型（全部整数算术）

记 `T = totalSupply`（份额总量），`A = totalAssets()`（库内资产，直接读
`asset.balanceOf(this)`，不维护可被捐赠污染的内部计数器）。

| 操作 | 公式 | 取整方向 |
|------|------|----------|
| 后续存款 | `shares = floor(assets · T / A)` | 向下（对存款者不利，对旧份额有利） |
| 赎回 | `assetsOut = floor(shares · A / T)` | 向下（对赎回者不利，零头留库） |
| 首次存款 | `shares = assets − 1000`，且 `T = assets` | 1:1 初始化 |
| 滑点下界 | `minOut = floor(estimated · (10000 − bps) / 10000)` | 向下 |

由此得到的核心不变量：

1. **存款不摊薄旧份额**：`shares·A ≤ assets·T`；
2. **集体偿付能力**：对任意份额分布，`Σ floor(sᵢ·A/T) ≤ A`，金库永不资不抵债；
3. **零份额保护**：当 `floor(...) = 0` 时存款直接回滚 `ZeroShares`，
   资产不会被白吞（“极小存款”攻击面）；
4. **空库初始化 / 死份额**：首次存款要求 `assets > 1000`，并把 `1000` 份额永久
   锁到 `address(1)`（Uniswap V2 式 MINIMUM_LIQUIDITY）。这让经典“首存 1 wei +
   巨额捐赠做空后续存款者”的通胀攻击**无利可图**——攻击者无法获得足够份额来
   放大捐赠收益；
5. **最大滑点**：每笔存/赎都带 `minShares` / `minAssetsOut`，实际结果低于下界即
   `SlippageExceeded` 回滚；
6. **恶意回调**：`deposit`/`redeem` 均为 `nonReentrant`，且严格遵循“检查-生效-
   交互”（先记账，最后才调用资产代币）。即使资产代币在 `transfer`/`transferFrom`
   中恶意回调，重入被拒、状态已结算。

> 设计权衡：死份额方案不能把后续存款者的相对损失降到 0，而是把它**钉死在
> `1000 / 首存总份额` 的固定比例**（首存金额越大比例越小），且与攻击者的捐赠
> 规模完全无关。测试用例 `test_firstDonationInflationAttack_isUnprofitable`
> 实测：攻击者捐赠 1,000,000 枚、最终只收回约 999 枚；紧随其后的正常存款者
> 损失约 0.0998%（=1000/1001），无论攻击者捐多少都不会再多亏。

---

## 5. 自动化测试

### 5.1 Foundry（单元 / 有界模糊 / 状态不变量 / 重入）

```bash
make forge-test          # 默认每用例 256 runs，不变量 256 runs × 500 calls
# 更高强度：
forge test --fuzz-runs 2000
```

覆盖与验收点对应关系：

| 验收要求 | 测试 |
|----------|------|
| 极小存款 | `test_tinyDeposit_zeroShares_reverts_andKeepsAssets`（0 份额回滚、资产不移动）；`testFuzz_depositRoundsDown` |
| 首次捐赠 | `test_firstDonationInflationAttack_isUnprofitable`、`test_donationToEmptyVault_mintsNoShares` |
| 全额赎回 | `testFuzz_fullRedeem_leavesDeadShares`、`testFuzz_singleUserRoundTripDust`、`testFuzz_redeemRoundsDown` |
| 恶意代币回调 | `MaliciousReentrancy.t.sol`：存款/赎回转账回调中重入均被拒 |
| 资产份额关系 | 4 条状态不变量（偿付能力、死份额恒定、汇率单调不减、铸销守恒），256×500 随机序列 |

### 5.2 Python / pytest（真实 Anvil 上的端到端 + HTTP）

```bash
make test
```

pytest 夹具会在独立端口 **8555** 上自动启动/复用 anvil（不影响你在 8545 上的
开发节点），每个用例重新部署合约。包含 6 个链上会计测试和 6 个 HTTP 接口测试。

### 5.3 实测结果

本节记录在本机（Linux x86_64, Foundry v1.8.3, Python 3.12.3）的真实运行结果，
详见 `TEST_RESULTS.md`：

- **Foundry：15/15 通过**（3 个测试合约；状态不变量 256 runs / 128,000 次调用，0 回滚失败）。
- **pytest：12/12 通过**（6 链上 + 6 HTTP，真实 Anvil）。
- 攻击用例实测数字：攻击者捐 1,000,000 → 收回 999.002；受害者损失 0.998 枚（存 1000 枚）。

---

## 6. HTTP 接口

服务启动后访问 `http://127.0.0.1:8000/docs` 可交互调试。所有金额字段均为
**资产/份额最小单位的整数**，JSON 中以 number 传输（web3/pydantic 内部为任意精度整数）。

| 方法 | 路径 | 说明 |
|------|------|------|
| GET  | `/health` | 链连接状态、chain id、最新区块 |
| GET  | `/vault/info` | 库内资产、总份额、定点汇率、死份额数 |
| GET  | `/vault/preview/deposit?assets=N` | 存款预览（向下取整 + 默认 0.5% 滑点下界） |
| GET  | `/vault/preview/redeem?shares=N` | 赎回预览 |
| GET  | `/account/{address}` | 资产余额 / 份额余额 / 授权额度 |
| POST | `/token/mint` | 水龙头（`{address, amount, private_key}`，只能给自己领） |
| POST | `/token/approve` | 授权金库 `{private_key, amount}` |
| POST | `/vault/deposit` | `{private_key, assets, receiver?, min_shares?, slippage_bps?}` |
| POST | `/vault/redeem` | `{private_key, shares, receiver?, owner?, min_assets_out?, slippage_bps?}` |

`min_shares` / `min_assets_out` 不传时，后端用 `slippage_bps`（默认 50bp=0.5%）
按整数公式自动计算下界并随交易提交，链上强制执行。

### curl 示例

```bash
PK0=0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80  # anvil #0
A0=0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266

curl -s http://127.0.0.1:8000/health
curl -s http://127.0.0.1:8000/vault/info
curl -s -X POST http://127.0.0.1:8000/token/mint -H 'content-type: application/json' \
  -d "{\"address\":\"$A0\",\"amount\":5000000000000000000000,\"private_key\":\"$PK0\"}"
curl -s -X POST http://127.0.0.1:8000/token/approve -H 'content-type: application/json' \
  -d "{\"private_key\":\"$PK0\",\"amount\":115792089237316195423570985008687907853269984665640564039457584007913129639935}"
curl -s "http://127.0.0.1:8000/vault/preview/deposit?assets=5000000000000000000000"
curl -s -X POST http://127.0.0.1:8000/vault/deposit -H 'content-type: application/json' \
  -d "{\"private_key\":\"$PK0\",\"assets\":5000000000000000000000}"
```

---

## 7. 安全说明与边界

- 私钥全部是 **Anvil 自带的公开测试密钥**，仅在本地开发链使用；后端不在任何日志中
  持久化私钥，但请求体中会携带——生产环境应改为托管签名 / 元交易。
- `MockERC20` 是无手续费、无回调的老实代币；`MaliciousERC20` 仅存在于测试中，
  用来证明金库不依赖资产代币“行为良好”。
- 已知未覆盖 / 刻意不做的事项见 `TEST_RESULTS.md` 的“未完成项 / 边界”。
