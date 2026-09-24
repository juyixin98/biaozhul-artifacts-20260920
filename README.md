# 链上检查点查值（On-chain Checkpoint Lookup）

按区块保存数值检查点的以太坊合约 + Python/FastAPI HTTP 服务。

- **同区块更新合并**：同一区块内多次写入只保留一个检查点（最后一次写入生效）。
- **历史查询**：`valueAt(targetBlock)` 返回**不晚于目标区块的最后一个值**；
  目标区块早于第一个检查点（或没有任何检查点）时返回 `0`。
- **拒绝未来块**：查询区块号大于链上当前区块时回滚（合约层）并返回 HTTP 400（接口层）。
- **查找算法**：链上对严格递增的区块号数组做**二分查找**（O(log n)）；
  另提供线性扫描参考实现（`NaiveCheckpoints` / `app/reference.py`）用于交叉验证与 gas 对比。

链环境**仅使用本机 Anvil 与公开测试私钥**，HTTP 接口只连接本地链。

---

## 1. 目录结构

```
src/
  Checkpoints.sol        # 二分查找实现（交付合约）
  NaiveCheckpoints.sol   # 线性扫描参考实现（仅用于测试/gas 对比）
test/Checkpoints.t.sol   # Foundry 单元/模糊/gas 测试
app/
  main.py                # FastAPI 应用与 HTTP 路由
  chain.py               # web3.py 合约客户端（读写、未来块判定）
  reference.py           # 纯 Python 线性扫描参考实现
  config.py              # 环境变量配置
scripts/
  deploy.py              # 部署合约
  demo.sh                # 端到端 HTTP 示例
  demo_same_block.py     # 同块合并示例（automine off）
tests/                   # pytest 集成测试（自动启动 Anvil、快照隔离）
requirements.txt         # 顶层依赖（已锁定版本）
requirements.lock        # 完整传递依赖锁定（pip freeze）
foundry.toml
Makefile
```

---

## 2. 依赖

| 组件 | 版本 | 说明 |
|---|---|---|
| Foundry（forge/anvil） | 1.8.3（solc 0.8.26） | 编译、单测、本地链 |
| Python | 3.12 | |
| web3.py | 8.0.0 | 链交互 |
| FastAPI | 0.141.1 | HTTP 框架 |
| uvicorn[standard] | 0.53.0 | ASGI 服务 |
| pytest | 9.1.1 | 集成测试 |
| httpx | 0.28.1 | TestClient / 示例 |

> forge-std 通过 `forge install foundry-rs/forge-std`（或 `git clone` 到 `lib/forge-std`）获取，
> `lib/` 已在 `.gitignore`。完整传递依赖见 `requirements.lock`。

### 安装

```bash
# Foundry（若未安装）
curl -L https://foundry.paradigm.xyz | bash && foundryup
export PATH="$HOME/.foundry/bin:$PATH"

# Python 依赖
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt   # 或 pip install -r requirements.lock

# forge-std 测试库
forge install foundry-rs/forge-std --no-git
```

---

## 3. 快速开始（本机三终端 / 也可用 Makefile）

```bash
export PATH="$HOME/.foundry/bin:$PATH"

# 终端 1：启动本地链（默认端口 8545）
anvil --host 127.0.0.1 --port 8545

# 终端 2：编译并部署
forge build
.venv/bin/python scripts/deploy.py
#   → 输出 contract: 0x5FbDB2315678afecb367f032d93F642f64180aa3

# 终端 3：启动 API
RPC_URL=http://127.0.0.1:8545 \
CONTRACT_ADDRESS=0x5FbDB2315678afecb367f032d93F642f64180aa3 \
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
```

或使用 Makefile：`make anvil` / `make deploy` / `make run`。

> 默认私钥使用 Anvil 打印的第一个测试私钥（公开值，**仅限本地**），可用 `PRIVATE_KEY` 覆盖。

---

## 4. HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 链连接、chainId、当前块、合约地址 |
| POST | `/checkpoints` | 当前块写入检查点，body：`{"value": <0..2²²⁴-1>}` |
| GET | `/checkpoints/{block}` | 不晚于 `{block}` 的最后值；未来块返回 400 |
| GET | `/latest` | 最新检查点 |
| GET | `/history` | 全部检查点（区块号、值），供链下核对 |

交互文档：`http://127.0.0.1:8000/docs`。

### curl 示例

```bash
# 写入（Anvil 中交易在「最新块 + 1」打包）
curl -s -X POST localhost:8000/checkpoints \
  -H 'Content-Type: application/json' -d '{"value":42}'

# 查询
curl -s localhost:8000/checkpoints/100
# {"targetBlock":100,"currentBlock":100,"value":42,"found":true}

# 未来块
curl -s localhost:8000/checkpoints/999
# HTTP 400
# {"detail":"target block 999 is in the future (current block: ...)",
#  "error":"future_block","requested":999,"current":...}
```

---

## 5. 测试

```bash
make test          # = forge test + pytest
# 或分别运行：
forge test -vv                       # Solidity 单测 / 256 次模糊 / gas
.venv/bin/pytest                     # 集成测试（自动拉起独立 Anvil，快照隔离）
```

### 验收场景覆盖

| 要求场景 | Foundry（链上） | pytest（HTTP/链下） |
|---|---|---|
| 空记录 | `test_EmptyRecord_QueriesReturnZero` | `test_empty_record` |
| 第一块 | `test_FirstCheckpoint` | `test_first_checkpoint` |
| 块间空隙 | `test_GapsBetweenBlocks` | `test_gaps_between_blocks` |
| 大量同块更新合并 | `test_ManyUpdatesSameBlock_Merge`（500 次） | `test_many_updates_same_block_merge`（100 笔）、`test_same_block_merge_raw`（60 笔） |
| 拒绝未来块 | `test_RevertWhen_QueryFutureBlock_*` | `test_future_block_rejected` |
| 二分 vs 线性参考 | `testFuzz_BinaryMatchesLinear`（256 随机序列） | `test_randomized_matches_reference`（6 seeds × 80 操作） |
| gas 随历史增长 | `test_Gas_*` | `test_gas.py`（estimate_gas 实测） |

---

## 6. 实测结果（本仓库环境，如实记录）

环境：Foundry 1.8.3 / solc 0.8.26、Python 3.12.3、web3 8.0.0、本机 Linux x86_64。

### 6.1 Foundry

```
14 tests, 全部通过（fuzz 256 runs）
```

查询 gas（最老目标块 = 线性扫描最坏情况；forge 内 gasleft 实测）：

| 检查点数 n | 二分 binary | 线性 linear |
|---:|---:|---:|
| 16  | 15,917 | 42,919 |
| 256 | 30,847 | 642,636 |

- n 从 16 → 256（16 倍）：**线性扫描 gas 增长约 14.9 倍**（42,919 → 638,019），近线性；
  **二分仅从 15,917 → 26,225**（差值约 1.0 万，来自约 4 次额外迭代 + 首次冷读检查点槽的一次性成本），呈对数级。
- n=256 时二分比线性便宜约 **20.8 倍**（30,847 vs 642,636）。

pytest 中以 `eth_call.estimate_gas` 的独立测量（每次新部署合约、含冷访问）：

```
n=  16  binary= 36,749  linear= 63,651   ratio=1.73x
n=  64  binary= 41,903  linear=182,691   ratio=4.36x
n= 256  binary= 47,057  linear=658,863   ratio=14.00x
```
（数字落盘于 `target/gas-report.json`；冷访问基数较大，故绝对数值与 forge warm 测量不同，但增长趋势一致。）

### 6.2 pytest 集成测试

```
tests/test_api_scenarios.py  8 passed
tests/test_crosscheck.py     7 passed   （6 seeds × 80 随机操作 + 60 笔同块）
tests/test_gas.py            4 passed
```
（合计 19 passed。`test_gas.py` 需真实建 256 块历史，约耗时 3 分钟。）

### 6.3 真实端到端示例（脚本实跑输出）

见 `docs-demo-output.txt`。要点：
- 空记录查询 → `value=0, found=false`；
- 写 10（块 2），挖 4 个空隙块后写 20（块 7）；空隙块 3/4/5 查询返回 10；
- 查询块 10（当前为 7）→ HTTP 400 `future_block`；
- 同块 50 笔更新（`scripts/demo_same_block.py`）→ `/history` 仅新增 1 个检查点，值为 50。

---

## 7. 设计说明与边界

- 检查点结构 `struct Checkpoint { uint32 fromBlock; uint224 value; }` 单槽打包；
  值上限 2²²⁴−1（超界回滚 `ValueTooLarge`）。`uint32` 区块号足够用到约公元 38000 年以后。
- 仅部署者（`owner`）可写（参考实现 `NaiveCheckpoints` 放开权限便于测试）。
- 写入不依赖链下状态；合并逻辑完全在合约内部，与交易提交路径无关。
- 未来块判定在**链下 API 与链上合约两处**都做（防御性），错误码 `future_block`。
- 服务与链之间为明文 HTTP，仅绑定 `127.0.0.1`，按需求不做任何外网/测试网连接。

## 8. 未完成项 / 已知限制

- 未做 Docker/CI 配置；测试需本机已安装 Foundry（pytest 会自行 `forge build` 并拉起 Anvil）。
- 未实现鉴权 / 速率限制 / 事件订阅（需求未要求；owner 已限制链上写权限）。
- 同块压测的 HTTP 路径会等待回执，因此同块批量提交示例直接走原始交易
  （`evm_setAutomine(false)` 后 `send_raw_transaction`），这是 Anvil 工作方式所致，非合约限制。
- `NaiveCheckpoints` 仅用于测试对照，**不应**在生产部署。
