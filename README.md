# 兑换路径精确报价（swap-router）

单链**离线**兑换路由器：从本地流动性池快照中，搜索**最多 3 跳**、**池不重复**的兑换路径，
逐跳执行常数乘积（Uniswap V2 风格）整数交换，扣除每跳显式成本后，以**最终整数输出最大**选优；
输出完全相同则按**池 ID 元组字典序**决胜。纯后端（Rust + Axum + SQLite），无前端。

> 不是边际价格相乘。每一跳都用该池自身的费率与向下取整规则真实计算，
> 第 N 跳取整后的净输出就是第 N+1 跳的输入。

---

## 1. 快速开始

需要 Rust 工具链（开发使用 1.98.1）。所有依赖已可离线构建（SQLite 通过 `bundled` 静态编译，无需系统 libsqlite3）。

```bash
# 编译
cargo build --offline --locked

# 启动服务（默认 127.0.0.1:8080，数据库 ./swap_router.db）
./target/debug/swap-router serve

# 可选：自定义地址 / 数据库
SWAP_ROUTER_ADDR=127.0.0.1:9000 SWAP_ROUTER_DB=/tmp/router.db \
  ./target/debug/swap-router serve

# 或用命令行参数
./target/debug/swap-router serve --addr 127.0.0.1:9000 --db /tmp/router.db
```

### 灌入示例快照（两种方式等价）

```bash
# 方式 A：CLI seed
./target/debug/swap-router seed --db /tmp/router.db --input examples/snapshot.json

# 方式 B：HTTP
curl -X POST http://127.0.0.1:8080/snapshots \
  -H 'content-type: application/json' \
  --data @examples/snapshot.json
```

### 请求报价

```bash
# 单跳（强制直连）
curl -X POST http://127.0.0.1:8080/quotes \
  -H 'content-type: application/json' \
  --data @examples/quote_direct.json

# 最多 3 跳、每跳扣除显式成本 100（输出代币最小单位）
curl -X POST http://127.0.0.1:8080/quotes \
  -H 'content-type: application/json' \
  --data @examples/quote_multihop.json
```

成功响应（节选）：

```jsonc
{
  "snapshot_id": 1,
  "content_hash": "c2332ee705e08294607d65ca7c2ec7dff673d23c6ac255400dbb16a628b0a38d",
  "token_in": "USDC",
  "token_out": "WETH",
  "amount_in": "1000000000",
  "hops": [
    {
      "hop": 1,
      "pool_id": "pool-usdc-weth",
      "token_in": "USDC",
      "token_out": "WETH",
      "amount_in": "1000000000",
      "reserve_in": "1000000000000",
      "reserve_out": "300000000000000000000",
      "fee_bps": 30,
      "gross_amount_out": "298802094311970964",
      "explicit_cost": "0",
      "net_amount_out": "298802094311970964"
    }
  ],
  "gross_amount_out": "298802094311970964",
  "final_hop_explicit_cost": "0",
  "net_amount_out": "298802094311970964",
  "min_output": "297308083840411109",   // 0.5% 滑点保护
  "paths_considered": 1,
  "feasible_paths": 1
}
```

---

## 2. 一键验收

```bash
# 自动化测试（28 个库单元测试 + 12 个真实 HTTP/SQLite 集成测试）
cargo test --offline --locked

# 端到端验收：自动选空闲端口起服务、灌入快照、校验全部关键行为
./scripts/acceptance.sh
```

`acceptance.sh` 覆盖：单跳逐跳依据复算、多跳结转与显式成本、客户端最小输出拒绝(422)、
精度不明(400)、零储备(400)、流动性不足/灰尘(422)、跳数不足(404)、等价路径按池 ID 决胜、
以及 256 位溢出拒绝(422)。

---

## 3. HTTP API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET  | `/healthz` | 健康检查 |
| POST | `/snapshots` | 入库一个**不可变**池快照，返回 `snapshot_id` 与内容哈希 |
| GET  | `/snapshots/latest` | 最新快照元数据 |
| GET  | `/snapshots/{id}` | 指定快照元数据 |
| POST | `/quotes` | 精确报价 |

### `POST /snapshots`

```json
{
  "assets": [
    { "id": "USDC", "decimals": 6 },
    { "id": "WETH", "decimals": 18 }
  ],
  "pools": [
    {
      "id": "pool-usdc-weth",
      "token0": "USDC",
      "token1": "WETH",
      "reserve0": "1000000000000",
      "reserve1": "300000000000000000000",
      "fee_bps": 30
    }
  ]
}
```

入库校验（任一失败返回 400，拒绝整份快照）：

* 资产 / 池 ID 非空且不重复；
* 池引用的每个代币都**必须在 `assets` 中声明精度**（精度不明即拒绝）；
* `decimals` 范围 0..=38（u128 最多约 38 位十进制整数）；
* 两个储备都**严格为正**（零储备拒绝）；
* `fee_bps ∈ [0, 10000)`；池两端代币不同。

所有金额使用**十进制字符串**传输，以完整覆盖 u128 范围（不依赖 JS 数字精度）。

### `POST /quotes`

```json
{
  "snapshot_id": 1,                 // 省略则使用最新快照
  "token_in": "USDC",
  "token_out": "WBTC",
  "amount_in": "50000000000",      // 必填，正整数（原始最小单位）
  "cost_per_hop": "100",           // 可选，默认 "0"；每跳输出代币扣除的固定成本
  "slippage_bps": 100,             // 可选，0..=10000；min_output = floor(net*(10000-bps)/10000)
  "min_output": "1000000",         // 可选；净输出低于此值直接 422，不给报价
  "max_hops": 3                    // 可选，1..=3，默认 3
}
```

错误码：

| HTTP | code | 触发条件 |
|------|------|----------|
| 400 | `BAD_REQUEST` | 参数非法、代币未知、同币对、金额非正、`max_hops` 越界、JSON 畸形 |
| 404 | `SNAPSHOT_NOT_FOUND` / `NO_PATH` | 快照不存在 / 跳数内结构上无路径 |
| 422 | `NO_FEASIBLE_PATH` | 路径存在但取整/成本后均无正输出或流动性不足 |
| 422 | `OUTPUT_BELOW_MINIMUM` | 最优净输出低于客户端 `min_output` |
| 422 | `OVERFLOW` | 交换中间结果超过 256 位（按 EVM revert 语义拒绝，不静默回绕） |

---

## 4. 交换数学（真实执行，非边际价格）

每个池都是常数乘积 `x*y=k`，单跳输出（Uniswap V2 公式）：

```
amount_in_with_fee = amount_in * (10000 - fee_bps)
numerator          = amount_in_with_fee * reserve_out
denominator        = reserve_in * 10000 + amount_in_with_fee
amount_out         = floor(numerator / denominator)
net_out            = amount_out - cost_per_hop
```

* 费率单位是基点：`fee_bps=30` 即 0.30%。
* 全程整数、每跳独立**向下取整**；多跳时把上一跳 `net_out` 原样作为下一跳 `amount_in`。
* 中间乘法使用自实现的**带检查 256 位整数**（`src/uint256.rs`，u128×u128→u256 经 64 位竖式乘法，
  除法为移位长除法）。任何加/乘溢出返回 `None` 并上抛为 `OVERFLOW`，绝不静默截断——
  与链上 uint256 溢出 revert 对齐。
* 单跳取整为 0、或显式成本 ≥ 该跳输出，则该路径不可行；搜索继续尝试其它路径。

### 选优与决胜

1. 在所有 1..=`max_hops` 跳、池不重复的可行路径中，取**最终净输出最大**者；
2. 最终净输出相等，取**池 ID 元组字典序最小**者（逐池比较，先比第一跳，再比第二跳……）。

### 剪枝与穷举参考

* 生产搜索为 DFS，并带**反向可达性剪枝**：预处理 `reach[k][token]`（目标在 ≤k 跳内是否可达，
  允许复用池，故为真实可行解的超集，剪枝绝不会误删可行路径）。
* 同文件提供 `brute_force_sequences`：**穷举**所有不重复池序列，逐条从起点独立 `simulate`。
* 测试 `randomized_small_graphs_prune_matches_brute_force` 在 400 个随机小图（3–5 代币、随机储备
  跨越多个数量级、随机费率、多组输入/成本/跳数）上，要求三套引擎——剪枝 DFS、不剪枝 DFS、
  穷举参考——给出**相同的 considered/feasible 计数与相同的获胜路径**，以此验证剪枝正确性。

---

## 5. 目录结构

```
Cargo.toml              依赖与锁定（rusqlite bundled / axum / sha2）
Cargo.lock              锁定依赖版本
src/
  uint256.rs            带检查的 256 位整数（乘/除/溢出）+ 单测
  amm.rs                常数乘积单跳公式（真实整数取整）+ Uniswap 参考向量单测
  model.rs              领域模型、JSON DTO、入库校验
  router.rs             图、DFS+反向可达性剪枝、决胜规则、结构性可达判定
  router_tests.rs       穷举参考对照 + 场景单测（环/精度/等价路径/流动性/溢出）
  db.rs                 SQLite 存储 + 真实 SHA-256 内容哈希 + RFC3339 时间
  service.rs            报价编排、滑点 min_output、最小输出校验
  lib.rs                库入口与 Axum 路由/错误映射（供集成测试）
  main.rs               CLI（serve / seed）薄入口
tests/http.rs           12 个真实 HTTP + SQLite 集成测试
examples/               示例快照与报价请求
scripts/acceptance.sh   一键端到端验收
```

---

## 6. 设计说明与边界

* **离线 / 单链**：只读本地快照，不访问任何链或外部价格源；快照一旦入库不可变。
* **内容哈希**：对「按 ID 排序的 assets/pools、固定字段序、紧凑 JSON」做真实 SHA-256
  （域前缀 `swap-router-snapshot-v1\n`）。报价回传该哈希，可据此核对报价所依据的储备/费率版本。
* **精度**：精度只用于入库校验，路由金额一律使用原始最小单位整数，不做任何臆造的小数换算。
* **显式成本语义**：`cost_per_hop` 以**该跳输出代币**的最小单位计；不同跳成本币种不同，
  因此响应不做跨币种相加，只在每跳给出 `explicit_cost`，顶层给末跳成本。
* **环**：允许路径中重复经过某代币（如三角环、平行费率档），仅禁止重复使用同一个池；
  环路径被诚实地枚举并参与比价，通常因多付手续费而自然落败。
* 并发：SQLite 连接置于 `tokio::Mutex` 后，写入走单事务（快照表/资产表/池表一起提交）。
