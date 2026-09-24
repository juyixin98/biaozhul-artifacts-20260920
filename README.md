# 链上检查点查值（On-chain Block Checkpoints）

按区块保存数值检查点的 Solidity 合约 + Python/FastAPI 查询服务。只使用**本机 Anvil**
与 Anvil 自带的公开测试密钥，HTTP 接口连接本地链。

## 功能语义

- **写**：`setValue(uint256)` 在当前区块记录一个值。
  - 同一区块内多次更新**合并为一个检查点**（末值生效，last write wins）；
  - 新区块的第一次更新才追加检查点（写入 O(1)，只动尾部槽位）。
- **查**：`getAtBlock(uint256 targetBlock)` 返回**区块号不晚于目标块**的最后一个
  检查点（upper-bound 二分查找，O(log n)）。目标块早于首个检查点或历史为空时返回
  `exists=false`。
- **未来块保护**：`targetBlock > block.number` 时回滚自定义错误
  `FutureBlock(requestedBlock, currentBlock)`；HTTP 层映射为 `400 future_block`。

## 目录结构

```
src/Checkpoints.sol          合约（检查点存储、同块合并、二分历史查询）
abi/Checkpoints.json         手工维护的最小 ABI（供 web3.py 使用）
test/Checkpoints.t.sol       Foundry 测试（功能 8 例 + gas 断言，零外部依赖）
script/GasBench.s.sol        gas 基准脚本，输出 GasTable 事件
app/config.py                配置（本地 RPC、Anvil 测试密钥）
app/contract.py              web3.py 合约封装、部署、FutureBlock 错误解码
app/reference.py             线性扫描参考实现（用于交叉核对二分结果）
app/main.py                  FastAPI 应用
scripts/deploy.py            部署脚本
scripts/start_anvil.sh       Anvil 启动脚本
tests/                       pytest：真实 Anvil 端到端 + HTTP + gas（19 例）
requirements.txt             直接依赖与版本范围
requirements.lock            pip freeze 锁定的完整版本
reports/                     实际测试运行记录与 gas 表
```

## 依赖

- **Foundry**（forge / anvil / cast），实测版本 1.8.3，solc 0.8.26（foundry 自动下载）
- **Python 3.12**，依赖见 `requirements.txt`，完整锁定见 `requirements.lock`：
  web3 7.16、FastAPI 0.141、uvicorn 0.53、pydantic 2.13、pytest 8.4、httpx 0.28
- 无需任何外部链、付费 RPC 或真实密钥

## 启动命令

### 1. 准备环境

```bash
# Foundry（若尚未安装）
curl -L https://foundry.paradigm.xyz | bash && foundryup
export PATH="$HOME/.foundry/bin:$PATH"

# Python 虚拟环境
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt        # 复现完全一致版本: pip install -r requirements.lock

# 编译合约（产出 out/，部署和 API 启动都需要）
forge build
```

### 2. 启动本地区块链

```bash
./scripts/start_anvil.sh
# 等价于: anvil --host 127.0.0.1 --port 8545 --chain-id 31337 --block-time 1
```

### 3. 部署合约

```bash
python scripts/deploy.py
# 输出 deployed at: 0x5FbDB2315678afecb367f032d93F642f64180aa3
```

### 4. 启动 HTTP 服务

```bash
CONTRACT_ADDRESS=0x5FbDB2315678afecb367f032d93F642f64180aa3 \
  uvicorn app.main:app --host 127.0.0.1 --port 8000
# 不设 CONTRACT_ADDRESS 时，服务启动会自动向本地链部署一个新合约（仅限本地开发）
```

环境变量：`RPC_URL`（默认 `http://127.0.0.1:8545`）、`CHAIN_ID`（默认 31337）、
`SENDER_PRIVATE_KEY`（默认 Anvil 第 0 个测试账户）、`CONTRACT_ADDRESS`。

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 链连接、链 ID、当前块、合约地址、检查点数 |
| POST | `/values` | body `{"value": <uint256>}`，当前块写值；返回块号、是否合并、tx hash |
| GET | `/values/latest` | 最新检查点；无记录返回 `{"exists":false,...}` |
| GET | `/values/at/{block}` | 不晚于 `{block}` 的最后值；未来块返回 400 |
| GET | `/checkpoints?offset=&limit=` | 原始检查点列表（分页，上限 1000） |

交互式文档：`http://127.0.0.1:8000/docs`。

### curl 示例

```bash
curl -s http://127.0.0.1:8000/health
curl -s -X POST http://127.0.0.1:8000/values -H 'Content-Type: application/json' \
  -d '{"value":100}'
curl -s http://127.0.0.1:8000/values/at/5
# {"exists":true,"block_number":2,"value":100}
curl -s -w '\nHTTP %{http_code}\n' http://127.0.0.1:8000/values/at/99999
# {"detail":{"error":"future_block","requested_block":99999,"current_block":7}}  HTTP 400
```

## 测试

测试会**自动启动并销毁**一个临时 Anvil（随机端口、`--silent`），无需手动开节点。

```bash
# Solidity：功能与 gas 断言
forge test -vv

# Python：真实 Anvil 端到端、HTTP、线性参考交叉核对、gas
pytest tests/ -v -s
```

### 验收场景覆盖

| 要求 | Foundry | pytest |
|---|---|---|
| 空记录（任意已发生块查询均 `exists=false`） | `test_EmptyHistory` | `TestEmpty.*`（含 HTTP） |
| 第一块（精确块查询、空隙后返回首值、首块之前为空） | `test_FirstBlock` | `TestFirstBlock.*` |
| 块间空隙（边界与空隙内部点，返回前值且带原区块号） | `test_BlockGaps` | `test_gaps_vs_linear_reference`、`test_http_gap_queries` |
| 大量同块更新（300/500 笔同块合并为 1 个检查点，末值生效） | `test_ManyUpdatesSameBlock`（500） | `test_batch_merges_to_one_checkpoint`（300，单块打包）、`test_http_batch_then_lookup`（200） |
| 拒绝未来块（合约回滚 + HTTP 400） | `test_RejectsFutureBlock` | `TestFutureBlock.*` |
| 合并后下一块正常追加 | `test_MergeThenAppend` | 上述场景隐式覆盖 |

### 线性扫描参考实现核对

`app/reference.py` 用**显式线性扫描**建模同一套语义（同块合并 + “不晚于目标块的
最后值”）。空隙场景中对每个边界/内部目标块都同时调用链上二分与参考线性扫描，
逐字段比对 `(exists, blockNumber, value)`（见 `_assert_matches_chain`）。

### gas 结果（实测）

**Foundry 确定性 EVM（`forge script script/GasBench.s.sol`，冷存储；完整表见
`reports/gas_benchmark.md`）：**

| 检查点数 n | 操作 | gas |
|---:|---|---:|
| 0 | 首次 append | 90,180 |
| 1 | append | 75,467 |
| 2 | 同块 merge | 33,390 |
| 200 | append | 30,590 |
| 200 | 同块 merge | 33,390 |
| 4 | 查询（冷） | 10,697 |
| 16 | 查询（冷） | 15,793 |
| 64 | 查询（冷） | 20,889 |
| 256 | 查询（冷） | 25,985 |
| 1024 | 查询（冷） | 31,081 |

**真实 Anvil 回执/估算（`reports/pytest_run.txt`）：**

- 写：n≈200 append 75,096，n≈400 append 75,108（差 12 gas，不随历史增长）；
  merge 恒定 33,019–33,031，且始终低于 append。
- 读：历史每扩大 **4 倍**，查询 gas 固定增加 **+5,096**（恰为 2 个额外二分时，
  每次约 2.5k），即 O(log n)；n=256 查询仅 46,690 gas（线性冷扫描需 >500k）。
- 同块 300 笔更新总 gas 9,960,194（平均 33,200/笔），全部落于同一区块、
  链上只有 1 个检查点。

gas 断言（合约测试与 pytest 各有一份）：merge 必须显著便宜于 append；append 成本
不随历史长度变化；查询历史翻倍的 gas 增量有上界（线性实现无法通过）。

## 设计说明与取舍

- 检查点结构为 `(uint64 blockNumber, uint256 value)`：每检查点两个存储槽，
  区块号与值都不做位打包，逻辑直白、便于审查；区块号占 8 字节足够远期使用。
- 二分采用标准 upper-bound：找到 `blockNumber <= target` 的最大位置后取前一位，
  天然满足“不晚于目标块的最后值”。
- 服务端在调用合约前先用 `eth_blockNumber` 做一次未来块预检，同时解码链上
  `FutureBlock` 错误数据，双保险返回结构化 400。
- 同块批量打包借助 Anvil 的 `anvil_setAutomine(false)` + `evm_mine`，用于测试与
  演示真实的“一个区块含多笔更新”（生产链上同块多笔交易同理自动合并）。

## 实测记录

见 `reports/`：

- `foundry_test_run.txt`：8/8 通过（2026-09-24，forge 1.8.3）
- `pytest_run.txt`：19/19 通过，约 97 秒（Python 3.12，web3 7.16）
- `gas_benchmark.md`：上表的完整生成数据

另做过真实进程的手动端到端验证（Anvil + uvicorn + curl）：空记录、首块写值、
4 个空块空隙后再写、空隙内逐块查询、未来块 400、负数 422、以及 web3 批量
10 笔同块更新合并为 1 个检查点（末值生效），行为全部符合预期。

## 未完成项 / 已知限制

- 无鉴权与速率限制：定位为本地练习项目，仅监听 127.0.0.1，使用公开测试密钥；
  切勿直接暴露到公网。
- 未包含 CI 配置与 Docker 镜像；测试可直接在装好 Foundry/Python 的环境一键运行。
- 查询仅按“最后值”语义，未提供区间遍历/事件订阅接口（`/checkpoints` 可分页查看
  原始检查点作为补充）。
- 合约无权限控制，任何人可写值（题目未要求 access control）；如需可加 owner。
- `forge fmt` 对 foundry.toml 中两个 fmt 键报 unknown warning，不影响编译与测试。
