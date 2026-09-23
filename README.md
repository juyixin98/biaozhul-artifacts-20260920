# 兑换路径精确报价（Exact-Path Swap Quote Router）

单链**离线**兑换路由器：从**本地池快照**中寻找最多 **3 跳**、**池不重复**的兑换路径，
每跳按自身费率与**整数地板舍入**真实执行，扣除每跳的**显式固定成本**后，
以最终**整数净输出最大**为目标选路；输出相同（平局）时按**池 ID 序列字典序**选优。

纯后端实现：**Rust + Axum + SQLite**，无前端。无任何外部价格源、无链上调用——
所有计算都在本地对快照数据真实执行。

## 核心语义

### 每跳精确整数公式（Uniswap V2 风格恒定乘积）

```
inAfterFee = amountIn * (10_000 - feeBps)                       // u128 checked_mul
grossOut   = floor( inAfterFee * reserveOut
                    / (reserveIn * 10_000 + inAfterFee) )       // U256 中间运算
netOut     = grossOut - costOut                                 // 显式成本，输出资产单位
```

- **逐跳独立地板取整**：上一跳的整数 `netOut` 是下一跳的 `amountIn`。
  **绝不**把边际价格（reserve 比值）跨跳相乘后再取整——那会丢失每跳的 floor，
  系统性高报输出。
- `feeBps` 是该池自身费率（基点，0..=10000）。
- `costToken0Out` / `costToken1Out` 是**按方向**的显式固定成本（输出资产最小单位），
  在费率换出之后、进入下一跳之前扣除；`grossOut < costOut` 该跳即失败。
- 金额在 API/DB 上一律是**十进制整数字符串**（最小单位，如 wei），JSON 数字一律拒绝，
  避免 JS 53 位精度截断。金额域为 `u128`；中间乘积用 `U256`/`U1024`，
  超出 `u128` 域的金额运算显式返回 `Overflow`，绝不回绕。

### 路径搜索

- DFS 枚举 1..=3 跳的有向边序列；**同一池不可重复使用**（token 可重复，因此三角形
  循环路径如 A→C→B、以及回到已访问 token 的路径都会被枚举）。
- 任意深度到达目标资产都可停下（1/2/3 跳同台比较）。
- 目标：净整数输出最大；平局取池 ID 序列字典序最小者。
- **安全剪枝**：每个 DFS 节点用一个「忽略池去重、忽略显式成本、忽略地板取整」的
  实数比值上界（U1024 分数精确比较）判断该子树是否可能超过当前最优。
  仅当乐观上界**严格小于**当前最优整数时才剪枝（平局不剪，以保留字典序更小的路径）。
  剪枝的正确性由 `tests/reference_parity.rs` 中小图**无剪枝穷举参考实现**对拍保证
  （数千个随机小图 + 手工对抗图 + 小储备/高费率边界图）。
- 同资产报价走 0 跳恒等路径（循环只会因费/成本损耗价值，不枚举）。

### 明确拒绝的情况

| 情况 | 行为 |
|---|---|
| 资产精度不明（引用了未声明资产，或对未声明资产报价） | 快照入库 `422 INVALID_SNAPSHOT`；报价 `422 ASSET_PRECISION_UNKNOWN` |
| 储备为零的池 | 入库拒绝 `422 INVALID_SNAPSHOT` |
| 金额/中间运算超 u128 | 该跳失败；若所有路径都因此不可行则 `422 NO_ROUTE`（边失败原因可见） |
| 无可达路径 / 流动性不足（成本超过产出、零产出） | `422 NO_ROUTE`，带 `paths_considered` 与 `edge_failures` |
| `amount_in` 为 0、非字符串、超 u128 | `400/422` 拒绝 |
| `slippage_bps` 超出 0..=10000 | `400 BAD_SLIPPAGE` |
| 未知字段（typo 防护） | `422`（`deny_unknown_fields`） |

### 快照 ID 与最小输出

- `snapshot_id = hex(SHA-256(canonical_json))`：资产与池按 ID 排序、字段顺序固定的
  规范化 JSON 再做真实 SHA-256（`sha2` crate，非占位）。相同内容重复上传幂等。
- 报价返回 `snapshot_id`、完整**逐跳依据**（每跳的入参、两侧储备、费率、扣费后输入、
  `gross_out`、`cost_out`、`net_out`，以及代入具体数值的公式字符串 `basis`）。
- `min_amount_out = floor(amount_out * (10_000 - slippage_bps) / 10_000)`。

## HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 健康检查 |
| POST | `/v1/snapshots` | 上传快照（校验+幂等），201 新建 / 200 已存在 |
| GET | `/v1/snapshots` | 列出全部快照 ID |
| GET | `/v1/snapshots/:id` | 读取快照原文 |
| POST | `/v1/snapshots/:id/quote` | 精确报价 |

报价请求体：

```json
{ "asset_in": "WETH", "asset_out": "USDC",
  "amount_in": "1000000000000000000", "slippage_bps": 50 }
```

## 本地启动

前置：Rust 工具链（开发于 1.98.1）。SQLite 由 `rusqlite` 的 `bundled` feature 从源码编译，
**无需系统安装 libsqlite3**。

```bash
cd /home/admin/Downloads/biaozhul/P016/a
cargo build --release
./target/release/quote-router                 # 默认 127.0.0.1:8080, ./router.sqlite
# 可选： --bind 127.0.0.1:9090  --db /tmp/q.sqlite
# 或环境变量 ROUTER_BIND / ROUTER_DB
```

## 验收命令（冒烟）

```bash
cargo test --release          # 全部自动化测试（约 10-20s，含随机穷举对拍）

# 启动服务（另开一个终端）
./target/release/quote-router --bind 127.0.0.1:8080 --db /tmp/accept.sqlite

BASE=http://127.0.0.1:8080

# 1) 上传示例快照，拿到 snapshot_id
curl -s -X POST $BASE/v1/snapshots -H 'content-type: application/json' \
  --data @examples/snapshot.json
SID=$(curl -s -X POST $BASE/v1/snapshots -H 'content-type: application/json' \
  --data @examples/snapshot.json | python3 -c 'import sys,json;print(json.load(sys.stdin)["snapshot_id"])')

# 2) 报价：1 WETH -> USDC（该图真实最优是 WETH->USDT->USDC 两跳）
curl -s -X POST $BASE/v1/snapshots/$SID/quote -H 'content-type: application/json' \
  --data @examples/quote_weth_usdc.json

# 3) 拒绝：零储备快照
curl -s -i -X POST $BASE/v1/snapshots -H 'content-type: application/json' \
  --data @examples/snapshot_invalid_zero_reserve.json
```

在本机实测（开发机真实输出，随 `examples/snapshot.json` 可复现）：

- 快照 ID：`22e68fd829215ab7d7f5a953c73680720709a1b7bd3bab3fb9b304203c136a08`
- `1 WETH -> USDC`（slippage 0.5%）选中两跳 `weth_usdt -> usdc_usdt`：
  - hop1 净得 `3075369284` USDT（30bps），hop2 净得 **`3073171708`** USDC（0.1bps），
    `min_amount_out = 3057805849`；同一快照上直接 `weth_usdc` 池给 `2988020943`
    （约 2988.02 USDC），故两跳胜出。
- 搜索统计：`paths_evaluated=3, branches_pruned=6, edges_attempted=12`。

## 自动化测试一览

```
cargo test
```

- `src/amount.rs`：整数字符串解析、拒绝 JSON 数字/符号/超 u128、U256 界转换。
- `src/swap.rs`：Uniswap V2 已知向量（100/1000/1000/30bps → 90）、零费率、
  显式成本扣减、成本>产出拒绝、100% 费率、u128 域内可达的溢出拒绝、零储备/零入金。
- `src/db.rs`：SQLite 存取与内容 ID 幂等。
- `tests/router_scenarios.rs`（15 个）：
  循环（三角形）、**不同精度**（6/18 位混存）、**多条等价路径**的池 ID 平局
  （含第二 ID 反序仍以第一 ID 判定）、**流动性不足**带边失败原因、断连、
  显式成本逐跳累加、3 跳链、池不重复、双向取边、以及「边际价连乘会得到 87.93…
  而链式整数 floor 必须报 87」的反高报用例。
- `tests/reference_parity.rs`（3 个）：
  - 3600+ 组随机小图（3–7 资产、2–8 池、混合费率/成本/精度），剪枝 DFS 与
    **完全无剪枝穷举**逐条对拍（输出金额 + 平局池序列 + 独立重放获胜路径）；
  - 手工构造的「短路看起来好、深路真正优」对抗图，并断言剪枝确实发生；
  - 1..30 微型储备边界对拍。
- `tests/http_api.rs`（15 个）：真实 Axum + 内存 SQLite 端到端：
  上传/幂等/列表/读取、快照 ID 与独立 SHA-256 一致、报价结构、滑点、
  各类 4xx（零储备、精度不明、NO_ROUTE 边失败、坏滑点、数字金额、超 u128、未知字段）。

## 目录结构

```
src/
  amount.rs    u128 Amount（整数字符串线格式）+ U256/U1024
  model.rs     Asset/Pool/Snapshot、校验、规范化 SHA-256
  swap.rs      单跳精确整数交换（费率/floor/显式成本/溢出）
  router.rs    图、DFS、可证明安全剪枝、最小输出；reference 子模块为穷举参考
  db.rs        SQLite 持久化（WAL，INSERT OR IGNORE 幂等）
  api.rs       Axum 路由与错误码
  lib.rs / main.rs
examples/      可直接 curl 的示例输入
tests/         场景、穷举对拍、HTTP 端到端
```

## 依赖（已锁定，见 Cargo.lock）

axum 0.7、tokio 1、rusqlite 0.31（bundled）、serde/serde_json、sha2 0.10、hex、
tracing/tracing-subscriber、uint 0.9（大整数）；开发依赖 tower、tempfile。

## 说明与取舍

- 「密码操作」按需求真实执行：快照 ID 使用真实 SHA-256；本服务无签名/鉴权场景，
  不伪造任何加密功能。
- 报价是对**不可变快照**的纯函数；同一快照 ID + 同一请求永远得到同一结果。
- 显式成本按方向建模（输出资产计费），因为不同精度资产的固定费无法用单一数值表达。
