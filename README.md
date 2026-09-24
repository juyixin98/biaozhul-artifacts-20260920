# Merkle 批量领取与位图（Merkle Claim &amp; Bitmap）

用 **Solidity + Foundry** 实现链上批量领取合约，用 **Python + FastAPI + web3.py** 实现
Merkle 证明生成与提交后端。链环境只用本机 **Anvil** 与公开测试密钥，HTTP 接口只连接本地链。

- 叶子绑定 **chainId + 合约地址 + index + account + amount**，杜绝跨链 / 跨合约 / 篡改重放；
- 按 **index 位图（BitMap）** 防重复领取；
- **批量领取原子执行**：一批中任意一项证明无效 / 已领 / 转账失败，整笔交易回滚，不留部分已领状态。

---

## 1. 目录结构

```
.
├── src/                        # Solidity 合约（无第三方依赖，自包含）
│   ├── MerkleClaim.sol         #   主合约：单笔/批量领取、位图、原子批次
│   ├── MerkleProof.sol         #   sorted-pair Merkle 证明校验
│   └── BitMaps.sol             #   按索引的位图库
├── test/                       # Foundry 合约单元测试（14 个）
│   ├── MerkleClaim.t.sol
│   ├── TestMerkle.sol          #   测试用建树库
│   └── RejectReceiver.sol      #   拒收 ETH 的合约（测转账失败回滚）
├── script/Deploy.s.sol         # 参考部署脚本（实际部署走 Python）
│
├── app/                        # FastAPI 后端
│   ├── main.py                 #   HTTP 路由 / 应用工厂
│   ├── merkle.py               #   叶子 + Merkle 树/证明（与合约逐字节一致）
│   ├── chain.py                #   web3.py：连接、地址预测、部署、单笔/批量领取
│   ├── allocations.py          #   分配表加载与校验（pydantic）
│   └── config.py               #   环境变量配置
├── scripts/deploy.py           # CLI 部署脚本（输出 deployment.json）
├── tests/                      # pytest 端到端（37 个，自动拉起 Anvil）
│   ├── test_merkle.py          #   纯 Python 树逻辑（11 种树形、域绑定）
│   ├── test_chain_integration.py  # 真实 Anvil：重放/重复/原子性
│   └── test_http_api.py        # FastAPI → Anvil 全 HTTP 流程
├── data/allocations.json       # 示例分配表（7 个领取者）
│
├── foundry.toml
├── requirements.txt            # 直接依赖（固定版本）
├── requirements-lock.txt       # 完整传递依赖锁（pip freeze，59 个包）
└── run_all_tests.sh            # 一键：forge build + forge test + pytest
```

---

## 2. 依赖

| 组件 | 版本 | 说明 |
|---|---|---|
| Foundry（forge/anvil/cast） | 1.8.3（本机已装于 `~/.foundry/bin`） | 编译、测试、本地链 |
| solc | 0.8.26（forge 自动下载） | 合约编译器 |
| Python | 3.10+（实测 3.12.3） | 后端 |
| web3.py | 6.20.3 | 连链 / 签名 / 发交易 |
| FastAPI | 0.115.6 | HTTP 接口 |
| uvicorn | 0.34.0 | ASGI 服务 |
| pydantic | 2.10.4 | 配置/入参/分配表校验 |
| eth-abi / rlp | 5.1.0 / 4.0.1 | 叶子编码、CREATE 地址预测 |
| pytest / httpx | 8.3.4 / 0.28.1 | 测试 |

> 合约侧**未引入 OpenZeppelin**：`MerkleProof` 与 `BitMaps` 都是等价的小型自包含实现
> （见 `src/`），减少外部代码供应链。`lib/forge-std` 是 `forge init` 的标准测试脚手架。

---

## 3. 快速开始

### 3.1 准备环境

```bash
# Foundry（如尚未安装）
curl -L https://foundry.paradigm.xyz | bash
foundryup
export PATH="$HOME/.foundry/bin:$PATH"

# Python 虚拟环境 + 锁定依赖
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-lock.txt   # 或 requirements.txt

# 编译合约（生成 out/，后端部署需要其中的 ABI/bytecode）
forge build
```

### 3.2 启动本地链

```bash
anvil --chain-id 31337                 # 默认 RPC http://127.0.0.1:8545
```

另开一个终端，启动后端（**用 uvicorn 的 --factory**）：

```bash
source .venv/bin/activate
RPC_URL=http://127.0.0.1:8545 \
uvicorn --factory app.main:create_app --host 127.0.0.1 --port 8000
```

### 3.3 部署合约

**方式一：HTTP 部署**

```bash
curl -s http://127.0.0.1:8000/health            # deployed=false
curl -s -X POST http://127.0.0.1:8000/deploy -H 'Content-Type: application/json' -d '{}'
# 自动：预测 CREATE 地址 → 以该地址为域造叶子和根 → 注入全部资金部署
```

**方式二：CLI 部署**

```bash
RPC_URL=http://127.0.0.1:8545 python -m scripts.deploy
# 部署信息（地址/根/资金）写入 deployment.json
```

> **先有地址还是先有根？** 叶子要绑定合约地址，但根又在构造函数里。
> 解决：部署前用 `keccak256(rlp([deployer, nonce]))[12:]` **预测 CREATE 地址**，
> 用预测地址造根，再以该 nonce 部署；测试断言预测地址 == 实际地址。

### 3.4 领取

```bash
# 查某索引的证明
curl -s http://127.0.0.1:8000/allocations/0/proof

# 单笔领取
curl -s -X POST http://127.0.0.1:8000/claim \
  -H 'Content-Type: application/json' -d '{"index":0}'

# 批量领取（原子）
curl -s -X POST http://127.0.0.1:8000/claim/batch \
  -H 'Content-Type: application/json' -d '{"indexes":[1,2,3]}'
```

### 3.5 配置（环境变量，均有默认值）

| 变量 | 默认 | 说明 |
|---|---|---|
| `RPC_URL` | `http://127.0.0.1:8545` | 本地 Anvil RPC |
| `DEPLOYER_PRIVATE_KEY` | Anvil 第 1 个测试密钥 | 部署/出资 |
| `CLAIMER_PRIVATE_KEY` | 同 deployer | 代发领取交易（任何人都可代领，钱只进叶子中的 account） |
| `ALLOCATIONS_PATH` | `data/allocations.json` | 分配表 |
| `ARTIFACT_PATH` | `out/MerkleClaim.sol/MerkleClaim.json` | 合约构建产物 |
| `CONTRACT_ADDRESS` | 空 | 已部署合约地址（设置后启动即连接，无需再 `/deploy`） |

---

## 4. HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 链连接、chainId、是否已部署、分配数量 |
| GET | `/allocations` | 全部分配项 |
| GET | `/allocations/{index}/proof` | 单项证明（含 `proof`、`claimed`、`root`） |
| POST | `/deploy` | 预测地址 + 造根 + 出资部署（body 可选 `{"fund_wei": "..."}`） |
| POST | `/claim` | 单笔领取 `{"index": 0}` |
| POST | `/claim/batch` | 批量领取 `{"indexes": [1,2,3]}` |

批量接口在发送前先 `eth_estimateGas`：任一项会失败时节点直接返回 revert，
后端返回 `422 {"tx_sent": false, ...}`，**交易根本不发出**，链上零变化。

---

## 5. 叶子与树（双语言必须逐字节一致）

```
leaf = keccak256(abi.encodePacked(
    bytes32(block.chainid),                    # 32B
    bytes32(uint160(address(this))),           # 32B（显式 bytes32）
    uint256 index,                             # 32B
    address account,                           # 20B ← packed 下 address 只占 20 字节！
    uint256 amount,                            # 32B
))                                             # 共 148 字节
```

内部节点：`keccak256(sorted(a,b))`（排序配对，证明无需区分左右）；
奇数层复制最后一个节点。Python（`app/merkle.py`）与 Solidity（`src/MerkleProof.sol`）
使用同一算法，并由跨语言测试共同验证。

> 开发中实际踩到并修复的一个坑：Solidity `abi.encodePacked` 会把 `address` 压成
> **20 字节**（不是普通 ABI 编码的 32 字节）。Python 端必须用 `int(account).to_bytes(20)`，
> 否则叶子哈希与链上不一致、所有证明失效。`app/merkle.py` 中有 `len == 148` 断言守护。

---

## 6. 自动化测试

一键运行（编译 + 合约测试 + 端到端测试，端到端会自动在 `8555` 端口拉起独立 Anvil）：

```bash
./run_all_tests.sh
# 或分步：
forge test -vv
pytest tests/          # TEST_ANVIL_PORT 可改端口
```

### 实测结果（本机，2026-09-24）

**Forge：14 passed, 0 failed**

| 测试 | 验收点 |
|---|---|
| `test_CrossContract_ReplayProofFromAtoB_Reverts` | 跨合约重放 A→B 失败 |
| `test_CrossContract_ReplayProofFromBtoA_Reverts` | 跨合约重放 B→A 失败 |
| `test_CrossChain_ProofForOtherChainId_Reverts` | 跨链（改 chainId）重放失败 |
| `test_SingleClaim_RevertWhenDuplicateIndex` | 重复索引失败 |
| `test_Batch_RevertWhenDuplicateIndexInsideBatch` | 批次内重复 → 整批回滚，无部分状态 |
| `test_Batch_RevertWhenOneProofInvalid_NoPartialState` | 批次单项证明无效 → 整批回滚 |
| `test_Batch_RevertWhenIndexAlreadyClaimedBeforeBatch` | 批次含已领索引 → 整批回滚 |
| `test_Batch_RevertWhenOneTransferFails_NoEthMoved` | 批次内转账失败 → 整批回滚，合约资金不变 |
| `test_SingleClaim_RevertWhenAmount/AccountTampered` | 篡改数量/账户失败 |
| `test_Batch_HappyPath` 等 | 正常单笔/批量、事件、位图正确 |

**pytest：37 passed, 0 failed**
- `test_merkle.py`：11 种树形（1/2/3/…/17 叶）的证明自洽、奇数层复制、叶子对
  chainId/合约/index/账户/数量五个域逐一绑定、空树拒绝、叶序敏感；
- `test_chain_integration.py`：在**真实 Anvil** 上验证 Python 证明被合约接受、
  CREATE 地址预测精确命中、跨合约重放被 `InvalidProof` 拒绝、单笔/批量重复回滚、
  单项无效/转账失败整批回滚且余额/位图无部分变化；
- `test_http_api.py`：FastAPI 全流程（部署幂等冲突、证明查询、单笔、批量、
  重复批次 `tx_sent=false`、未部署 409）。

### 真实服务手动冒烟（已实际执行）

`anvil(8546) + uvicorn(8001)`：部署 → 单笔领 0 → 重复领 0 返回 **422** →
批量领 `[1,2,3]` 成功（一笔交易）→ 批量 `[4,4]` 返回
`422 {"tx_sent": false}` 且索引 4 仍 `claimed=false`；
用 `cast` 把合约 A 的 index0 证明原封不动发到合约 B，节点返回：

```
execution reverted: custom error 0xf25ea2d5 ... InvalidProof(0, 0x7099…C8, 1e18)
```

---

## 7. 安全模型说明

- **不可伪造性**：只有分配表中的 `(index, account, amount)` 能构成被根接受的叶子，
  证明由后端按表生成；代领者无法改变收款账户（账户在叶子里）。
- **重放边界**：chainId + 合约地址取自链上上下文（`block.chainid`、`address(this)`），
  调用方无法提供，故跨链、跨合约证明天然失效。
- **防重**：位图按 index 置位，先查后写；批量内重复 index 第二次命中断言回滚。
- **原子性**：`claimBatch` 先在循环里完成全部「校验+置位」，再在第二个循环里统一转账；
  任一环节 revert，EVM 回滚整笔交易的所有状态与 ETH 转移。
- **出资**：构造时 `payable` 注入资金，可用 `receive()` 补资；只出不进控制逻辑之外无特权函数，
  合约**没有 owner / 提现函数**（演示用，生产中如需可暂停/提款应另行加入并明确权限）。

---

## 8. 未完成项 / 已知限制（如实记录）

1. **无管理提现/紧急暂停**：资金一旦注入只能按 Merkle 表领取；演示范围未做 owner 提款。
2. **分配表为静态 JSON**：换表需重新部署合约（根不可变，by design）。未做根轮换 / 多期支持。
3. **代发交易的 gas 由 `CLAIMER` 支付**；`claim` 不做提交者白名单（任何人可代领，
   资金仍只流向叶子账户，安全但代领成本公开）。
4. **未做形式化证明 / fuzz 不变量测试**（`StdInvariant` 可后续补充「总领取量 ≤ 总注资」
   之类不变量）；现有 fuzz 仅在 Foundry 默认配置下运行。
5. **未做容器化 / CI 工作流文件**：提供了 `run_all_tests.sh`，GitHub Actions 可直接调用。
6. 后端为单实例内存态；多副本/重启续用请设置 `CONTRACT_ADDRESS` 指向已部署合约
   （分配表与根须一致），未实现数据库持久化。
7. 测试用 Anvil 固定测试密钥，**切勿在主网或任何真实资金环境使用**。
