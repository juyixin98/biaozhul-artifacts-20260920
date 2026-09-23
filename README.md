# 区间锁死锁检测服务（Rust + Axum）

纯后端、内存态的**事务区间锁（range lock）服务**。区间统一为**左闭右开**
`[start, end)`，支持共享（S）/排他（X）锁、每资源 FIFO 等待队列、事务
中止，并在每次请求进入等待时基于**等待图（waits-for graph）**做确定性的
死锁检测与牺牲者选择。

- 语言/框架：Rust（edition 2021）+ [Axum](https://github.com/tokio-rs/axum) 0.7 + Tokio
- 存储：纯内存（`BTreeMap` + `tokio::sync::Mutex`），重启即清空，无外部依赖

---

## 1. 快速开始

### 依赖

- Rust 工具链（cargo ≥ 1.75 即可，开发使用 stable 1.x）
- 无需数据库、无需 Docker；构建时需要访问 crates.io 拉取依赖

### 启动

```bash
cargo run --release                 # 默认监听 0.0.0.0:3000
RANGE_LOCK_PORT=8080 cargo run     # 自定义端口
```

看到 `range-lock service listening on http://0.0.0.0:3000` 即启动成功。

### 运行测试

```bash
cargo test
```

---

## 2. HTTP 接口

所有请求/响应均为 JSON。加锁接口是**非阻塞**的：拿不到锁时请求进入 FIFO
等待队列，响应 `queued: true`；之后其他事务 commit/abort 会自动推进队列、
授予等待者。

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/txn/:id/begin` | 开启事务 |
| `POST` | `/txn/:id/locks` | 请求加锁；body：`resource, mode, start, end` |
| `POST` | `/txn/:id/commit` | 提交，释放全部锁并推进队列 |
| `POST` | `/txn/:id/abort` | 显式中止，释放全部锁并推进队列 |
| `GET`  | `/txn/:id` | 查询事务状态、持锁、等待中的请求 |
| `GET`  | `/txns` | 全部事务及状态 |
| `GET`  | `/resources` | 全部资源的持锁列表与等待队列 |
| `GET`  | `/waits` | 当前等待图的全部边 |
| `GET`  | `/health` | 健康检查 |

`mode` 取 `shared`（或 `s`）/ `exclusive`（或 `x`）。

### 加锁响应

```jsonc
{
  "txn": "A",
  "resource": "r2",
  "mode": "exclusive",
  "start": 0,
  "end": 10,
  "granted": true,        // 是否已获得锁（含升级兑现）
  "queued": false,        // 是否仍在等待队列
  "upgraded": false,      // 是否为当场完成的 S->X 升级
  "deadlock": {           // 本次入队检测到死锁时存在
    "cycles": [["A", "B"]],
    "victims": ["B"]
  }
}
```

- 无死锁：`200`
- 发生死锁但牺牲者是**别的事务**：`200`，响应里带 `deadlock`，并如实报告
  调用者自己最终是 `granted` 还是仍 `queued`
- 调用者**本人**被选为牺牲者：`409 Conflict`（响应体结构相同，
  `granted/queued` 均为 false）
- 事务不存在：`404`；区间非法（`start >= end`）或模式非法：`400`；
  对已中止事务操作：`409`

---

## 3. 语义说明（实现要点）

### 区间与兼容矩阵

- 区间半开：重叠 ⇔ `a.start < b.end && b.start < a.end`。因此 `[0,10)` 与
  `[10,20)` **相邻但不重叠**，互不阻塞。
- 锁模式仅 **S/S 兼容**；S/X、X/S、X/X 在重叠区间上均冲突。

### 等待队列与“真实阻塞”

每个资源一条 FIFO 队列。等待者 `w` 可被授予 ⇔ 同时满足：

1. 没有任何**其他事务**持有与 `w` 重叠且模式冲突的锁；
2. 队列中排在 `w` **前面**的等待者里，没有与 `w` 重叠且冲突的请求。

第 2 条保证公平（后到的 X 不能越过队首的等待者），但与全队都不冲突的
请求（例如不同区间）仍可直接授予，不会被无意义地挡住。

等待图边与上述两个条件**一一对应**，因此图里的每条边都代表真实阻塞：

- `holder` 边：等待者 → 持有冲突锁的事务；
- `ahead` 边：等待者 → 队列前方的冲突等待者。

这避免了“有边但其实没被挡住”的假边，从而避免死锁误报。

### 死锁检测与确定性牺牲者

每次请求进入等待后：重建等待图 → Tarjan 求强连通分量（SCC）→
大小 ≥ 2 的 SCC 即死锁环。牺牲者确定性地取**所有环节点中事务 id
字典序最大者**（图遍历按 id 排序，环输出也排序，结果完全可复现）。
牺牲者被中止：释放它在**所有资源**上持有的锁、删除它的全部等待请求，
然后推进各资源队列，使其余事务继续。

### 锁升级（S → X）

事务已持 S 再请求重叠区间的 X：无阻碍则当场升级（保守地把该事务在此资源
上的锁合并为覆盖两段区间的 X 锁，`upgraded: true`）；否则升级请求入队
（`upgrade` 标记），等待期间**保留原 S 锁**，并正常参与等待图与死锁
检测——两个互相等对方释放 S 的升级请求会构成二环并被解开。

### 已知简化

- 内存态、单实例、无持久化；
- 加锁 API 非阻塞（不挂起 HTTP 请求），靠查询/后续请求观察授予结果；
- 升级按覆盖区间保守合并，不做精确的区间拆分；
- 中止事务保留 `aborted` 墓碑：在重新 `begin` 同 id 前对其加锁/提交会
  返回 409；再次 `begin` 同 id 会自动清除墓碑并开启新事务。

---

## 4. 请求样例（curl）

仓库提供 `examples/demo.sh`，依次演示：共享锁共存 → 排他等待 →
二环死锁 → 三环死锁 → 相邻不重叠不误报 → 锁升级。也可手动执行：

```bash
curl -s localhost:3000/health

# 开启两个事务
curl -s -XPOST localhost:3000/txn/A/begin
curl -s -XPOST localhost:3000/txn/B/begin

# A 拿 r1 的 X，B 拿 r2 的 X
curl -s -XPOST localhost:3000/txn/A/locks -H 'content-type: application/json' \
  -d '{"resource":"r1","mode":"exclusive","start":0,"end":10}'
curl -s -XPOST localhost:3000/txn/B/locks -H 'content-type: application/json' \
  -d '{"resource":"r2","mode":"exclusive","start":0,"end":10}'

# B 等 r1（queued=true，无死锁）
curl -s -XPOST localhost:3000/txn/B/locks -H 'content-type: application/json' \
  -d '{"resource":"r1","mode":"exclusive","start":0,"end":10}'

# A 请求 r2 => 二环；B 为最大 id，被中止；A 当场获得 r2
curl -s -XPOST localhost:3000/txn/A/locks -H 'content-type:application/json' \
  -d '{"resource":"r2","mode":"exclusive","start":0,"end":10}'

curl -s localhost:3000/waits       # 等待图已清空
curl -s localhost:3000/resources   # r1 无主，r2 归 A
curl -s -XPOST localhost:3000/txn/A/commit
```

---

## 5. 测试覆盖

- 单元测试（`src/engine.rs` 内）：
  - 半开区间重叠/相邻语义、非法区间与模式；
  - S/S 共存；相邻不重叠 X 锁零等待边（**无误报**）；
  - **二环**：检出、牺牲者确定性、释放后对方继续；
  - **三环**：检出、链式推进（C 中止 → B 获锁 → B 提交 → A 获锁）；
  - FIFO 防插队、队列边的真实性；不冲突区间可绕过无关等待者；
  - **锁升级**：当场升级、升级冲突入队、升级双环检测；
  - 牺牲者跨多资源全部释放；中止事务的拒绝/墓碑清理。
- HTTP 集成测试（`tests/http_api.rs`）：经 Axum router 端到端覆盖上述
  四个验收场景及 400/404/409 状态码。

---

## 6. 实际运行记录

以下结果在交付环境（Ubuntu 24.04 x86_64，Rust 1.98.1 stable）真实执行所得。

### 自动化测试（`cargo test`）

```
running 13 tests  (src/engine.rs 单元测试)
... 全部 ok
test result: ok. 13 passed; 0 failed

running 5 tests  (tests/http_api.rs HTTP 集成测试)
test http_adjacent_ranges_no_false_positive ... ok
test http_health_and_lifecycle ... ok
test http_upgrade ... ok
test http_two_cycle ... ok
test http_three_cycle ... ok
test result: ok. 5 passed; 0 failed
```

合计 **18 个测试全部通过**，覆盖：区间半开/相邻语义、S/S 共存、
**二环**、**三环**、**相邻不重叠零误报**、FIFO 防插队、
**锁升级（含升级双环）**、多资源牺牲者释放、多牺牲者循环消解、
400/404/409 状态码。

### HTTP 示例（`examples/demo.sh` 真实输出摘要）

- 共享锁：A `[0,10)` S 与 B `[5,15)` S 同时 `granted=true`；C 同区间 X
  `queued=true`。
- 二环：B 等 r1 时仅 `queued`；A 再请 r2，响应
  `"deadlock":{"cycles":[["A","B"]],"victims":["B"]}`，A 当场 `granted`，
  `/waits` 随即为空。
- 三环：C 闭环时响应
  `"cycles":[["A","B","C"]],"victims":["C"]`（HTTP 409）；`/waits` 仅剩
  `A -> B`；B 提交后 A 获 rB、图清空。
- 相邻区间：`[0,10)`、`[10,20)`、`[20,30)` 三把 X 全部立即授予，`/waits`
  返回 `[]`（**无误报**）。
- 锁升级：A、B 共享后 A 升级 X 入队；B 也升级 X 时检测出二环，B 牺牲，
  `/resources` 显示 r 上仅剩 A 的一把 `exclusive` 锁。
- 状态码实测：本人为牺牲者 `409`、对已中止事务操作 `409`、未知事务
  `404`、`start>=end` `400`、非法 mode `400`。

### 依赖与可复现性

- `Cargo.lock` 已随源码提交，锁定 axum 0.7.9 / tokio 1.x / serde 1.x 等
  全部直接与间接依赖版本。
- 交付环境访问 crates.io 较慢，构建时通过 USTC sparse 镜像
  （`--config source.crates-io.replace-with=ustc` 等）拉取依赖；
  在可直连 crates.io 的环境直接 `cargo build`/`cargo test` 即可，镜像
  配置不影响 `Cargo.lock`。

### 未完成项 / 已知限制

- **无持久化**：全部状态在内存，进程重启即清空；未做崩溃恢复。
- **非阻塞加锁 API**：加锁请求不挂起 HTTP 连接，拿不到锁立即返回
  `queued=true`，需通过 `/waits`、`/resources` 或后续请求观察授予结果；
  未提供长轮询/通知机制。
- **区间升级为保守合并**：S→X 把同事务在该资源上的锁合并成覆盖两段的
  X 锁，未做精确的区间拆分与回收。
- **单机单实例**：全局一把 `tokio::sync::Mutex` 保护引擎，无分片、无
  分布式锁；压测与性能优化未做。
- 未实现锁超时、按条件查询等扩展能力。

