# 事件回滚索引器（Event-Rollback Indexer）

在本地 Anvil 上对 Solidity 合约事件做**重组感知（reorg-aware）索引**：按块哈希记录
父链与日志位置，支持**确认深度**、**重组回滚**、**回滚后重新追赶**，业务状态由事件
**可逆更新**。HTTP 接口连接本地链，提供查询与手动同步。

- 合约：Solidity `0.8.26`（Foundry / forge）
- 链：本机 [Anvil](https://book.getfoundry.sh/anvil/)（`--no-mining` 手动出块）+ 测试密钥
- 后端：Python 3.12 · FastAPI · web3.py · SQLite（标准库）
- 全程只连 `http://127.0.0.1` 本地 RPC，无任何外部链 / 真实密钥

---

## 1. 它解决什么问题

链的头部可能重组。朴素索引器按块号覆盖会出错：

1. 孤块上的事件必须**撤销并移除**；
2. **同一笔交易**（同 tx hash）可能被两次打包进**不同块哈希**——不能因为 tx hash
   相同就认为已处理，也不能把两份业务效果叠加；
3. 重启 / 重放后，索引结果必须等于「只对最终规范链重放」的结果。

本项目通过三条设计保证正确：

- **哈希链而不是块号链**：每个已索引块存 `(number, hash, parent_hash)`，轮询时沿
  `parentHash` 回溯到与库中一致的**分叉点**。
- **日志主键含块哈希**：事件唯一键是 `(block_hash, tx_hash, log_index)`。同 tx 在不同
  块哈希下是不同行，回滚孤块时整行删除，绝不混淆。
- **可逆投影（reducer）**：每个事件携带完整增量，`Deposited` 为 `+amount`，
  `Withdrawn` 为 `-amount`；回滚一个块 = 按日志逆序施加反操作。归零时删除余额/总额行，
  使「重组后收敛」与「空库重放规范链」**逐字节一致**。

重组处理在**单个 SQLite 事务**内完成（回滚孤块 + 追上新链），崩溃只会停留在旧链或新链，
不会处于中间态。

---

## 2. 目录结构

```
src/Vault.sol               示例事件源合约（Deposited / Withdrawn）
contract-test/Vault.t.sol   forge 合约测试（无 forge-std 依赖）
app/
  abi.py        ABI + 事件解码（web3 process_log）
  storage.py    SQLite：块哈希链 / 事件 / 物化业务状态 / meta
  reducer.py    事件 -> 业务状态 的正/逆投影
  onchain.py    Anvil/web3 封装：部署、签名交易、手动挖矿、evm_snapshot/revert、取日志
  indexer.py    核心：确认深度、分叉点回溯、回滚 + 追赶（SyncReport）
  main.py       FastAPI：查询接口 + 后台轮询线程
scripts/
  deploy.py     向已运行的 Anvil 部署 Vault
  demo.py       自启 Anvil，造两层重组并打印全过程
tests/          pytest：单元 / 两层重组验收 / 确认深度 / 重启持久化 / HTTP API
requirements.in             直接依赖
requirements.lock.txt       pip freeze 锁定的完整版本（58 个包）
foundry.toml
```

---

## 3. 依赖与一次性准备

需要：`forge/anvil`（Foundry）与 Python 3.10+（实测 3.12）。
若 `anvil` 不在 PATH，测试 / 演示会用 `~/.foundry/bin/anvil`，也可用环境变量
`ANVIL_BIN` 覆盖。

```bash
# 1) 编译合约（生成 out/Vault.sol/Vault.json，部署与测试都依赖它）
forge build

# 2) Python 虚拟环境 + 锁定依赖
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.lock.txt      # 复现实测版本
# 或 pip install -r requirements.in       # 仅按区间安装直接依赖
```

合约自身测试：

```bash
forge test
```

---

## 4. 启动（本地链 + HTTP 服务）

终端 A —— 起链（保留自动出块即可用于演示；测试内部用 `--no-mining`）：

```bash
anvil --host 127.0.0.1 --port 8545
```

终端 B —— 部署合约，拿到地址：

```bash
. .venv/bin/activate
forge build
python scripts/deploy.py
# => 0x5FbDB2315678afecb367f032d93F642f64180aa3（确定性地址）
```

终端 C —— 起 HTTP 索引服务（环境变量即配置）：

```bash
. .venv/bin/activate
RPC_URL=http://127.0.0.1:8545 \
DATABASE_PATH=data/indexer.db \
VAULT_ADDRESS=0x5FbDB2315678afecb367f032d93F642f64180aa3 \
START_BLOCK=0 \
CONFIRMATION_DEPTH=1 \
POLL_INTERVAL=2 \
  uvicorn app.main:app --host 127.0.0.1 --port 8000
```

> 若你的机器走本地代理（如 clash），请对本地地址绕过代理，否则 curl 可能被代理拦截：
> `export no_proxy="127.0.0.1,localhost"`。

### HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 存活检查 |
| GET | `/config` | 当前配置 |
| GET | `/status` | 链头、确认深度、已索引 tip、块/事件计数、上次同步报告/错误 |
| POST | `/sync` | 立即触发一次「回滚 + 追赶」，返回 `SyncReport` |
| GET | `/balance/{address}` | 某地址的索引内余额（wei，字符串） |
| GET | `/totals` | `{deposited, withdrawn, net_locked}` |
| GET | `/events?who=&limit=` | 事件列表（按块倒序） |
| GET | `/debug` | 完整块哈希链 + 事件计数 + 余额 + 总额（测试用） |

快速验证：

```bash
curl -s http://127.0.0.1:8000/totals
# 用测试密钥打一笔存款（anvil 自动出块后，最多一个 POLL_INTERVAL 即被索引）
cast send --private-key 0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d \
  --rpc-url http://127.0.0.1:8545 $VAULT_ADDRESS "deposit()" --value 123
curl -s -X POST http://127.0.0.1:8000/sync
curl -s http://127.0.0.1:8000/totals
```

---

## 5. 自动化测试

pytest 会**自动在随机端口启动一个 `anvil --no-mining`**，每个用例用创世
`evm_snapshot/evm_revert` 隔离，结束自动关闭链。无需手动开 Anvil。

```bash
. .venv/bin/activate
python3 -m pytest
```

测试矩阵：

- `test_unit.py`：reducer 正/逆投影往返、中间块回滚、禁止负余额、**同 tx 不同块哈希可
  共存**、块链与 meta（纯 SQLite，不起链）。
- `test_reorg.py`（验收）：
  - 正向同步物化状态；
  - **确认深度**：未达 K 个确认的块不索引；
  - **两层重组**：分叉点在块 3，孤块 4u/5u/6u 三个块被整体回滚、规范块 4c/5c/6c 被追上；
    校验孤块事件**物理删除**、业务状态等于规范链；
  - **同交易不同块不混淆**：`nonce/to/value/data/gas` 完全相同的交易在两条分支哈希相同，
    孤块上位于块哈希 `H_u`、规范上位于 `H_c`（用 `evm_setNextBlockTimestamp` 制造不同块
    哈希），重组后只保留 `H_c` 下的一行；
  - **重启=规范链重放**：空库新建索引器重放规范链，块链 / 余额 / 总额 / 全部事件与重组收敛
    后的库逐项相等，并与链上 `stats()`、`balanceOf()` 一致。
- `test_restart_and_api.py`：**文件型 SQLite 关闭后重开**（模拟重启）状态保持，并与空库
  重放一致；以及基于 FastAPI TestClient 的 HTTP 端到端（含经 `/sync` 触发的重组）。
- `test_smoke.py`：部署 / 存款 / 出块 / 取日志最小链路。

一键演示（自启 Anvil、造两层重组、模拟重启）：

```bash
python scripts/demo.py
```

---

## 6. 核心算法（indexer.sync_to）

1. 目标块号 `target = chain_head - confirmation_depth`（低于起始块则不动作）。
2. 首次运行在 `START_BLOCK` 锚定一块（只建哈希链根，不要求事件）。
3. 取目标块，沿 `parentHash` 逐级向上，直到命中库中同号且同哈希的块——**分叉点**。
4. 从当前 tip 到分叉点**逐块回滚**：逆序撤销事件状态 → 删除该块事件行 → 删除块行。
5. 从分叉点**逐块前向应用**到目标块：插入块行 → 拉该块 Vault 日志 → 解码、按
   `(tx_index, log_index)` 排序 → 幂等插入（已存在的不重复记账）→ 正向投影。
6. 更新 tip。第 3–5 步包在一个数据库事务里。

> 关于确认深度：它只决定「索引到多深」，不改变重组算法。即便一个已确认块被更深重组替换
> （同高度不同哈希），下一次 `sync_to` 仍会沿父哈希回溯并回滚；`poll_once` 在
> `target == tip_number` 时也会执行同步以捕获这种同高度替换。

---

## 7. 实际运行结果（如实记录）

记录于开发机（Linux x86_64，Foundry 1.8.3，Python 3.12.3，web3 8.0.0）。

- `forge test`：**3 passed**（存款、取款、超额取款回滚）。
- `python3 -m pytest`：**12 passed**（见 `tests/`；唯一告警是 starlette TestClient 关于
  httpx 的弃用提示，不影响功能）。
- `python scripts/demo.py`：**DEMO OK**。两层重组中 `rolled back [6,5,4]`、
  `fork point #3`、同 tx 仅余规范块 4 下一行；重启后 tip 与余额保持，并与链上
  `stats()` 一致（deposited=475, withdrawn=0）。
- 真实进程联调：独立 `anvil` + `uvicorn` 启动，存款 123 后 `/status` 显示
  `events_indexed=1`、`/totals` 为 `deposited=123`、`/balance` 为 `123`（非 TestClient）。

### 已知边界 / 未完成项（如实说明）

- 重组若**深于 `START_BLOCK`**（连锚点块本身都变了）会明确抛错而非静默处理；本地 Anvil
  夹具不会产生该情形，生产上应把起始点选在足够深的 finalized 区域。
- 重组替换块时仅通过 `eth_getLogs` + 块拉取做全量对账，未做日志 bloom 预筛 / 增量缓存；
  对本地与中小区间足够，超大范围可按块窗口分批。
- HTTP 查询层无鉴权、无分页游标（`/events` 仅 `limit`），定位为本地演示后端。
- 仅索引 `Vault` 的两种事件；多合约 / 多事件注册表可在 `onchain.vault_logs` 的地址过滤与
  `abi.decode_log` 的 topic 分发表上扩展。
- 业务状态用整数 wei 存为文本；未处理自定义通证 / 多资产。
