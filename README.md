# Merkle 批量领取与位图（Merkle Claim & Bitmap）

用 **Solidity + Foundry** 实现链上批量领取合约，用 **Python + FastAPI + web3.py** 实现连接
**本机 Anvil** 的证明生成/提交后端。

叶子哈希绑定五个字段，防止跨链、跨合约、跨条目重放；用**按索引位图**防重复领取；
`claimBatch` 采用 **checks-effects-interactions** 两阶段执行，任一条目无效则整笔交易回滚，
**绝不留下部分已领状态**。

---

## 1. 目录结构

```
.
├── src/
│   ├── MerkleClaim.sol       # 主合约：叶子绑定、位图防重、原子批量领取
│   └── MerkleProof.sol       # 有序配对的 Merkle 证明校验库（无外部依赖）
├── test/
│   ├── MerkleClaim.t.sol     # 20 个 Foundry 测试（含重放/原子性/重入/ERC20）
│   ├── helpers/              # TreeBuilder（链上建树）、ClaimFactory（CREATE 地址预测）
│   └── mocks/                # MockERC20、ReentrantClaimant（重入攻击者）
├── backend/
│   ├── merkle.py             # 叶子编码 + Merkle 树（与合约逐字节一致）
│   ├── store.py              # 内存分配表与证明查询
│   ├── chain.py              # web3.py：连接本地链、构造/发送 claimBatch
│   ├── main.py               # FastAPI 路由
│   ├── schemas.py / config.py
├── deploy/deploy.py          # 预测 CREATE 地址 → 绑定地址生成根 → 部署
├── tests/                    # pytest：13 个纯单元 + 9 个 HTTP→链 端到端
├── examples/allocations.json # 示例分配表（8 项）
├── scripts/demo.sh           # 一键演示
├── requirements.txt          # 直接依赖（钉版本）
└── requirements.lock         # 完整传递依赖锁定（pip freeze）
```

---

## 2. 安全设计（如何防重放 / 防重复 / 保原子）

### 2.1 叶子绑定五字段
```solidity
leaf = keccak256(abi.encodePacked(
    block.chainid,   // 链 ID：分叉到别的链即失效
    address(this),   // 合约地址：同链第二个实例无法使用本实例的证明
    index,           // 领取索引
    account,         // 收款账户：改账户即失效
    amount           // 数量：改金额即失效
));
```
链下 Python（`backend/merkle.py:encode_leaf`）按完全相同的字段顺序与 packed 编码生成，
单元测试用 `eth_abi.packed.encode_packed` 独立交叉验证。

- **跨链重放**：`deployChainId` 在构造时写入 immutable，每次领取先校验 `block.chainid`；
  即便绕过，叶子里的 chainId 也与目标链根不匹配。
- **跨合约重放**：叶子含 `address(this)`。同链部署的第二个合约实例地址不同，
  拿第一个实例的证明去领，重算叶子对不上根 → `InvalidProof`。

### 2.2 按索引位图防重复
```solidity
mapping(uint256 => uint256) _claimedBitmap;
// 第 index 位：word = index >> 8，mask = 1 << (index & 0xff)
```
单项 `claim` 与批量 `claimBatch` 共用同一套「先查位图→校验证明→置位」逻辑，因此：
- 跨交易重复领取 → `AlreadyClaimed`；
- 同一批次内出现重复索引 → 第二次置位前即 `AlreadyClaimed`，整笔回滚。

### 2.3 批量原子执行
`claimBatch` 分两阶段：
1. **Checks & Effects**：循环校验全部条目（链 ID、位图、Merkle 证明）并置位——此阶段无外部调用；
2. **Interactions**：全部通过后才逐条付款（原生币 `call` / ERC20 `transfer`）。

任一条目证明无效、重复、或付款失败，EVM 都会回滚整笔交易，已置位的位图与已转出的资金全部撤销。
位图在付款前置位，也使得**收款回调中的重入**无法重复领取（见两个重入测试）。

> 资金模型：合约在部署时预存发放资金（构造函数 `payable`，`receive()` 仅用于注资）。
> `claim/claimBatch` 本身**非 payable**，领取交易 `value = 0`，资金从合约余额出。
> owner 可用 `withdraw` 紧急回收未领取资金。

---

## 3. 环境与依赖

| 组件 | 版本 | 说明 |
|---|---|---|
| Solidity | 0.8.26 | `foundry.toml` 固定，forge 自动下载 solc |
| Foundry | v1.8.3（forge/anvil/cast） | 安装见下 |
| Python | 3.12（3.10+ 即可） | 建议 venv |
| 直接依赖 | web3 7.9.0 / fastapi 0.115.6 / uvicorn 0.34.0 / pydantic 2.10.4 / eth-abi 5.1.0 / eth-account 0.13.4 / pytest 8.3.4 | 见 `requirements.txt`，完整锁定见 `requirements.lock` |

只使用 **Anvil 自带测试私钥**（公开已知，仅限本地！）：
`0xac09...ff80`，HTTP 仅连 `http://127.0.0.1:8545`。

### 安装 Foundry
```bash
# 官方方式
curl -L https://foundry.paradigm.xyz | bash && source ~/.bashrc && foundryup
# 验证
forge --version && anvil --version
```

### 安装 Python 依赖
```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt          # 或 -r requirements.lock 完全复现
```

---

## 4. 启动与使用

### 4.1 启动本地链
```bash
anvil --port 8545 --chain-id 31337
```

### 4.2 编译并部署
```bash
# 首次需要安装 forge-std 测试库（主合约本身零外部依赖）
[ -d lib/forge-std ] || forge install --no-commit foundry-rs/forge-std@v1.9.4
forge build
.venv/bin/python -m deploy.deploy \
  --rpc http://127.0.0.1:8545 \
  --allocations examples/allocations.json \
  --fund-eth 20 \
  --out deployments/anvil.json
```
部署脚本解决「叶子绑定合约地址」的循环依赖：CREATE 地址只由 `(deployer, nonce)` 决定，
可提前预测 → 用预测地址生成 Merkle 根 → 再部署，部署后断言实际地址一致。
输出 JSON 含 `address / root / chain_id / token / tx`。

### 4.3 启动后端
```bash
RPC_URL=http://127.0.0.1:8545 \
CONTRACT_ADDRESS=0x<部署输出的address> \
PRIVATE_KEY=0xac09...ff80 \
ALLOCATIONS_FILE=examples/allocations.json \
.venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```
（若不设 `CONTRACT_ADDRESS`，会自动读取 `deployments/anvil.json`。）
交互式文档：<http://127.0.0.1:8000/docs>

### 4.4 HTTP 接口

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/health` | 链连接状态、chainId、当前块 |
| GET | `/status` | 合约地址、token、owner、链上 root、分配项数 |
| POST | `/allocations` | 装载分配表（返回后端 root 并与链上 root 比对） |
| GET | `/allocations` | 查看分配表 |
| GET | `/proof/{index}` | 查询单项叶子与 Merkle proof |
| GET | `/verify/{index}` | 本地叶子 vs 合约 `leafHash`、是否已领（只读） |
| POST | `/batch/prepare` | 给索引列表生成证明与 `claimBatch` calldata（**不上链**） |
| POST | `/batch/submit` | 生成证明并发送原子批量领取交易 |

示例：
```bash
curl -s localhost:8000/status | python3 -m json.tool
curl -s localhost:8000/proof/0 | python3 -m json.tool

# 原子批量领取索引 0,1,2
curl -s -X POST localhost:8000/batch/submit \
  -H 'content-type: application/json' -d '{"indices":[0,1,2]}'

# 重复索引（1 已领）→ 整笔回滚，400，索引 3 不被部分领取
curl -s -X POST localhost:8000/batch/submit \
  -H 'content-type: application/json' -d '{"indices":[1,3]}'
```

### 4.5 一键演示
```bash
bash scripts/demo.sh
```

---

## 5. 自动化测试

### 5.1 Foundry（合约层，无需手动起链，内置 EVM）
```bash
forge test -vvv
```
覆盖：单项领取、跨合约/跨链重放、跨交易/批内重复索引、批次单项无效整体回滚、
大索引位图（index=300）、两类重入攻击、ERC20 模式成功/证明失败/付款失败回滚、owner 提款。

### 5.2 pytest（单元 + 真实 HTTP→Anvil 端到端）
```bash
# 夹具会自动：起 Anvil(8546) → forge build → 部署+注资 → 起 uvicorn(8011)
.venv/bin/python -m pytest -v
```
- `tests/test_merkle_unit.py`：叶子编码与 `eth_abi` 对照、各种树规模的证明正确性、
  篡改证明/换根失败、chainId 与合约地址绑定、重复 index/leaf 拒绝。
- `tests/test_e2e_claim.py`：真实 HTTP 成功/重复路径、prepare 不改状态、
  链上批次单项篡改原子回滚（核位图+核余额）、批内重复索引回滚、
  第二合约实例跨合约重放失败、另起 chainId=5555 的 Anvil 验证跨链重放失败/正确链成功。

> ERC20 模式：把 `--token 0x<ERC20>` 传给部署脚本即可；测试用 `MockERC20` 覆盖。

---

## 6. 实际运行结果

见 **[RESULTS.md](RESULTS.md)**，其中如实记录了本次环境中 `forge test` 与 `pytest` 的
真实输出、手动演示结果，以及已知限制/未完成项。
