# Event Rollback Indexer（事件回滚索引器）

本地 Anvil 链上的 Solidity 合约事件索引后端。按**块哈希**记录父链与日志位置，
支持确认深度、链重组（reorg）时按**事件逆效果**回滚业务状态，并在分叉切换后
重新追赶；重启后索引结果等于对当前规范链（canonical chain）从零重放。

- **合约**：Solidity `0.8.26` + Foundry（`Ledger`：Deposit / Withdraw / Transfer，事件可逆）
- **链**：仅本机 [Anvil](https://book.getfoundry.sh/anvil/) 与公开测试私钥，HTTP JSON-RPC
- **后端**：Python 3.12 + FastAPI + web3.py + SQLite（WAL）

---

## 1. 工作原理

### 1.1 数据模型（SQLite）

| 表 | 作用 |
|---|---|
| `blocks` | 每个见过的块：`number, hash, parent_hash, log_count, canonical`。孤块保留为 `canonical=0` |
| `events` | 事件主键是 **`(block_hash, tx_hash, log_index)`**，不是 `(tx_hash, log_index)` |
| `balances` | 业务物化状态：规范链事件正向折叠的账户余额 |
| `meta` | 已索引规范链 tip `(number, hash)` |

> 同一笔原始交易（同 `tx_hash`）可能被挖进两个竞争块。以块哈希作为主键的一部分，
> 两份事件行才不会互相覆盖或混淆；回滚时只移除/逆用旧块上的那份。

### 1.2 同步循环（`app/indexer.py`）

每个 tick（后台轮询或 `POST /sync`）：

1. 读链上 head。
2. **找分叉点**：从 `min(已索引tip, chainHead)` 向下，比较“库里规范块哈希”与“链上哈希”，
   直到二者一致。
3. **回滚**：从 tip 到分叉点逐块 `detach`——按 `log_index` **逆序**对每事件施加**逆效果**，
   删除该块事件行，块头标记为孤块（保留，不删）。
4. **追赶**：从分叉点 +1 到 head 逐块抓取日志并按序施加正效果。曾被 detach 的块若再次成为
   规范块，直接复用其孤块头（`readopt`），不产生重复行。
5. 块/事件/余额的写入在**单个 SQLite 事务**内，崩溃不会留下不一致状态。

### 1.3 可逆业务效果（`app/effects.py`）

| 事件 | 正效果 | 逆效果 |
|---|---|---|
| `Deposited(acct, amt)` | `bal[acct] += amt` | `bal[acct] -= amt` |
| `Withdrawn(acct, amt)` | `bal[acct] -= amt` | `bal[acct] += amt` |
| `Transferred(from,to,amt)` | `from -= amt; to += amt` | `to -= amt; from += amt` |

回滚会做余额非负不变量检查，违例即报错并回滚事务。

### 1.4 确认深度

`confirmed_tip = head - (N-1)`，仅作为读接口的“软最终性”标记（`confirmed` 字段）。
回滚路径本身不受深度限制：即便重组跨过确认边界也能正确逆操作。

---

## 2. 目录结构

```
contracts/
  src/Ledger.sol          # 业务合约（三个可逆事件）
  test/Ledger.t.sol       # Foundry 合约测试（无第三方库依赖）
app/
  config.py               # 环境变量配置
  abi.py                  # Ledger 最小 ABI
  chain.py                # web3.py 客户端 + 手工日志解码（topic0 + data words）
  effects.py              # 事件正/逆效果
  store.py                # SQLite 存储（ingest / detach / readopt）
  indexer.py              # 分叉点检测、回滚、追赶
  main.py                 # FastAPI 接口 + 后台轮询线程
scripts/deploy.py         # 部署 Ledger 到本地 Anvil
tests/                    # pytest：无链单测 + Anvil 验收测试
requirements.txt          # 直接依赖
requirements.lock         # 完整锁定版本（pip freeze）
foundry.toml
```

---

## 3. 依赖

- [Foundry / Anvil](https://book.getfoundry.sh/)（本仓库开发时为 `1.8.3`）。
  已在 `~/.foundry/bin` 则无需安装；否则：
  ```bash
  curl -L https://foundry.paradigm.xyz | bash && foundryup
  export PATH="$HOME/.foundry/bin:$PATH"
  ```
- Python 3.12，venv 安装锁定依赖：
  ```bash
  python3 -m venv .venv && source .venv/bin/activate
  pip install -r requirements.lock
  ```

> 仅使用 Anvil 自带的公开测试私钥，目标仅限 `127.0.0.1` 本机节点。

---

## 4. 启动命令

开三个终端（也可手动指定端口）。

**终端 A — 本地区块链：**
```bash
export PATH="$HOME/.foundry/bin:$PATH"
anvil --host 127.0.0.1 --port 8545
```

**终端 B — 部署合约 + 启动索引器 HTTP 服务：**
```bash
source .venv/bin/activate
forge build                       # 产出 contracts/out/Ledger.sol/Ledger.json
LEDGER=$(INDEXER_RPC_URL=http://127.0.0.1:8545 python -m scripts.deploy | grep -o '0x[0-9a-fA-F]\{40\}')
echo "$LEDGER"
INDEXER_RPC_URL=http://127.0.0.1:8545 LEDGER_ADDRESS=$LEDGER \
INDEXER_DB_PATH=data/indexer.db INDEXER_CONFIRMATIONS=2 \
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```
服务启动即开始后台轮询（`INDEXER_POLL_INTERVAL`，默认 1s）。也可随时
`POST /sync` 立即同步一次。

**终端 C — 发交易（可选）：**
```bash
export PATH="$HOME/.foundry/bin:$PATH"
K0=0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80
cast send $LEDGER "deposit(uint256,uint256)" 1000 101 \
  --rpc-url http://127.0.0.1:8545 --private-key $K0
```

### 环境变量

| 变量 | 默认值 | 含义 |
|---|---|---|
| `INDEXER_RPC_URL` | `http://127.0.0.1:8545` | 本地链 HTTP RPC |
| `LEDGER_ADDRESS` | 无（必填，服务需要） | 合约地址 |
| `INDEXER_DB_PATH` | `data/indexer.db` | SQLite 文件 |
| `INDEXER_START_BLOCK` | `0` | 首次索引起始块 |
| `INDEXER_CONFIRMATIONS` | `2` | 确认深度 N |
| `INDEXER_POLL_INTERVAL` | `1.0` | 后台轮询秒数 |
| `INDEXER_LOG_CHUNK` | `100` | getLogs 分块大小 |
| `INDEXER_AUTO_START` | `1` | 设 `0` 关闭后台轮询（只手动 `/sync`） |
| `DEPLOYER_KEY` | Anvil key #0 | 部署用测试私钥 |

---

## 5. HTTP 接口

| 方法 路径 | 说明 |
|---|---|
| `GET  /healthz` | 链连通性、链上 head、已索引 tip、确认 tip、轮询状态、最后错误 |
| `POST /sync` | 立即执行一次同步（返回 fork 点、detach/ingest 的块与事件数） |
| `GET  /status` | tip、确认高度、块/事件计数、上次同步报告 |
| `GET  /blocks?canonical_only=true` | 规范块列表（含每块 `confirmed`） |
| `GET  /blocks/orphans` | 保留下来的孤块头 |
| `GET  /events?canonical_only=&account=&confirmed_only=` | 事件（块号、tx、logIndex、解码字段、confirmed） |
| `GET  /state?account=` | 物化余额（规范事件折叠结果） |

---

## 6. 自动化测试

```bash
export PATH="$HOME/.foundry/bin:$PATH"
source .venv/bin/activate

forge test          # Solidity 合约测试
python -m pytest    # 全部 Python 测试（自动拉起临时 Anvil，随机端口）
```

- 无 Anvil 也能跑的纯单测：`tests/test_store_unit.py`、`test_indexer_unit.py`
  （内存假链驱动两层重组）、`test_effects_unit.py`
- 需 Anvil 的验收测试（标记 `@pytest.mark.anvil`，找不到 anvil 自动 skip）：
  - `test_reorg.py` —— **两层重组**验收
  - `test_same_tx.py` —— **同交易不同块不混淆**
  - `test_restart.py` —— **重启后索引 == 规范链重放**
  - `test_confirmations.py` —— 确认深度
  - `test_api.py` —— HTTP 接口

### Anvil 分叉夹具的关键细节

Anvil 的 fork 是**惰性回源**：分叉节点读取历史块时会向其 fork 上游请求。
因此夹具必须保持**无环的分叉 DAG**：

```
BASE（不可变）  ──fork──► X（永不 reset）
       └──────fork──► Y（永不 reset）──fork──► Z
O（被索引的 observer）只做 anvil_reset：X@4 → Y@4 → Z@5 → Y@5
```

若让 A 先 fork B、再把 B reset 反向 fork A，会形成 A↔B 回源环，历史块 RPC
会挂起。测试用 `anvil_reset` 切换 observer 的 fork 上游来精确制造分叉，
分支节点用 `--no-mining` + `evm_mine` 手工出块，保证块高确定。

---

## 7. 验收场景说明（tests/test_reorg.py）

```
BASE:  1(deploy) 2(deposit 1000)
X:                3(+700)            4(-70)
Y:                3'(+800)           4'(transfer 80)
Z:                                   5(+9000)        (fork Y@4)
Y 延伸:                              5(+5000)

observer: X@4 → Y@4   第1层重组：detach X3/X4（孤块事件移除）
          → Z@5        Z1 入规范链
          → Y@5        第2层重组：detach Z5，采纳 Y5
```

断言：孤块块头保留但其事件行移除；规范标签集合不含被弃分支事件；
余额 `f39F.. = 1000+800-80+5000 = 6720`、`7099.. = 80`；孤块共 3 个。
