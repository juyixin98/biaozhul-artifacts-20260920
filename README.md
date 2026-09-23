# 区间锁死锁检测服务（Rust + Axum）

纯后端的**事务区间锁服务**：区间为整数轴上的左闭右开区间 `[start, end)`，支持
**共享锁（S）/ 排他锁（X）**、**FIFO 等待队列**、**事务提交/中止**，并在每次请求入队时
基于**真实等待关系**构造等待图（Wait-For Graph）做死锁检测，**确定性**地选择牺牲者。

无界面，仅提供 HTTP/JSON 接口。

## 模型与规则

- 区间：`[start, end)`，`i64`，要求 `start < end`。两个区间重叠当且仅当
  `a.start < b.end && b.start < a.end`，因此**相邻区间**（如 `[0,10)` 与 `[10,20)`）
  **不重叠、不冲突**。
- 锁模式：`shared`（共享，多读）与 `exclusive`（排他，写）。S-S 兼容，其余组合在区间
  重叠时冲突；不重叠的锁永不对立。
- 等待队列：单个全局 FIFO 队列。新请求被以下两类事务阻塞：
  1. 持有冲突授予锁的事务（`reason = "holder"`）；
  2. 队列中**排在它前面**的冲突请求所属事务（`reason = "queue"`，严格 FIFO，禁止插队）。
- 等待图（WFG）边 `from -> to` 由每个等待请求**当前真实的阻塞者集合**导出，
  并标注阻塞原因。没有真实阻塞就没有边，因此**不会误报死锁**。
- 死锁检测：请求入队后，从该请求的事务出发做 DFS，若能回到起点则存在环。
  由于“每次入队都检测”，任何新产生的环必然包含最新入队者。
- 牺牲者选择（确定性）：**环中事务 id 最大者**被中止（事务 id 由 `/txn` 单调分配，
  从 1 开始）。中止会释放该事务的**全部锁**、移除其排队请求，然后重新处理队列，
  其他事务随即继续推进。
- 每个事务至多有一个进行中的（等待）请求；同一请求重复提交是幂等的。
- 锁升级：事务持有 S 后再请求重叠的 X 时，**自身不会阻塞自身**；是否等待只取决于
  *其他*事务是否还持有/排队冲突锁。

## 依赖与环境

- Rust（stable，开发时使用 1.98.1）、Cargo。安装：`curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh`
- 运行期 crate（版本锁定见 `Cargo.lock`）：
  `axum 0.7`、`tokio 1`（full）、`serde 1`、`serde_json 1`、`tracing`、`tracing-subscriber`；
  测试额外使用 `tower 0.4`（`util` 特性，用于 HTTP 内聚测试）。

## 构建与启动

```bash
cargo build --release
BIND_ADDR=127.0.0.1:3000 cargo run --release
# 看到：interval-lock-server listening on http://127.0.0.1:3000
```

`BIND_ADDR` 缺省为 `127.0.0.1:3000`。

## 自动化测试

```bash
cargo test
```

- `tests/lock_manager.rs`：锁管理器单元/集成测试（16 个，含 3 个异步测试）。
- `src/main.rs` 中的 `http_tests`：axum 端到端 HTTP 测试。

覆盖：二环、三环、相邻/不重叠区间无误报、无环不误报、锁升级、FIFO 公平性、
显式中止释放全部锁、阻塞 API 的授予/死锁中止/超时、事务生命周期校验。

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/txn` | 开启事务，返回 `{"txn":1}` |
| GET  | `/txn/:id` | 查看事务（状态、持有锁、等待请求、等待谁） |
| POST | `/txn/:id/commit` | 提交：释放全部锁并唤醒等待者 |
| POST | `/txn/:id/abort` | 中止：释放全部锁并唤醒等待者 |
| POST | `/lock` | 申请锁（可阻塞） |
| POST | `/unlock` | 释放一把**精确匹配**的锁 |
| GET  | `/state` | 全量快照：事务、队列、等待图、事件日志 |
| POST | `/reset` | 清空全部状态（测试/演示用） |

`POST /lock` 请求体：

```json
{
  "txn": 1,
  "mode": "exclusive",          // 或 "shared"
  "start": 0,
  "end": 10,                    // [start, end)
  "block": false,               // true 时 HTTP 阻塞直到授予/中止/超时
  "timeout_ms": 5000            // block=true 时的超时
}
```

响应：

- `200 {"status":"granted"}`
- `202 {"status":"waiting","waiting_for":[2]}`（未阻塞模式下入队等待）
- `409 {"status":"deadlock","victim":2,"cycle":[1,2,1]}`（本请求触发检测；
  牺牲者已被中止并释放全部锁，队列已重新处理，其他事务继续）
- `408 {"error":"timeout", ...}`（阻塞模式超时，等待请求自动出队）
- 错误：`400` 区间非法；`404` 事务/锁不存在；`409` 事务已结束、已有不同等待请求等。

`GET /state` 中 `wait_for` 的每条边形如
`{"from":2,"to":1,"reason":"holder"}`（或 `"queue"`）。

## 请求样例（curl）

仓库内 `examples/requests.sh` 可直接运行（需服务已启动，并要求安装 `curl`、`jq`）：

```bash
bash examples/requests.sh
```

手动执行（**二环死锁**）：

```bash
curl -s -XPOST localhost:3000/txn            # -> {"txn":1}
curl -s -XPOST localhost:3000/txn            # -> {"txn":2}
curl -s -XPOST localhost:3000/lock -H 'content-type: application/json' \
  -d '{"txn":1,"mode":"exclusive","start":0,"end":10}'
curl -s -XPOST localhost:3000/lock -H 'content-type: application/json' \
  -d '{"txn":2,"mode":"exclusive","start":20,"end":30}'
# T2 等 T1
curl -s -XPOST localhost:3000/lock -H 'content-type: application/json' \
  -d '{"txn":2,"mode":"exclusive","start":0,"end":10}'   # 202 waiting
# T1 再等 T2 -> 成环，牺牲者为 id 更大的 T2；T2 释放全部锁，T1 获得 [20,30)
curl -s -XPOST localhost:3000/lock -H 'content-type: application/json' \
  -d '{"txn":1,"mode":"exclusive","start":20,"end":30}'  # 409 deadlock victim=2
curl -s localhost:3000/state | jq .wait_for                # []
```

阻塞模式（T3 阻塞等待，T1 提交后自动授予）：

```bash
curl -s -XPOST localhost:3000/txn                        # -> {"txn":3}
curl -s -XPOST localhost:3000/lock -H 'content-type: application/json' \
  -d '{"txn":3,"mode":"exclusive","start":0,"end":10,"block":true,"timeout_ms":10000}' &
curl -s -XPOST localhost:3000/txn/1/commit               # 上面的请求随即返回 granted
```

## 实测结果（本机记录，2026-09-23，Rust 1.98.1，Linux x86_64）

- `cargo test`：**20/20 通过**（锁管理器 18 个，含 3 个异步测试；HTTP 端到端 2 个），
  `cargo clippy --all-targets` 无警告，`cargo test --locked` 通过（依赖锁定可复现）。
- `cargo build --release` 成功。
- `bash examples/requests.sh` 对真实运行的服务逐条执行，结果符合预期：
  - 二环：`T2 等 T1` 返回 `202 waiting, waiting_for:[1]`；`T1 等 T2` 返回
    `409 deadlock, cycle:[1,2,1], victim:2`；此后 `wait_for=[]`，T1 拿到 `[20,30)` 并提交。
  - 三环：`cycle:[4,3,5,4]`，`victim:5`（环中最大 id）；T5 释放全部锁，T3 获得 `[20,30)`，
    T4 在 T3 提交后继续获得 `[0,10)`。
  - 相邻区间：`[0,10)`、`[10,20)`、`[20,30)` 三把 X 全部 `200 granted`，`wait_for=[]`。
  - 锁升级：P、Q 同持 S 时，P 升级 X 返回 `202 waiting_for:[9(Q)]`；Q 提交后 P 获得 X。
  - 阻塞 API：等待方在持锁方提交后约 1s 返回 `200 granted`；超时（700ms）返回
    `408 timeout`，等待请求自动出队，随后事务可正常发起新请求。

> 备注：交付环境直连 crates.io 极慢，构建时在 **CARGO_HOME（非仓库内）** 使用了
> `rsproxy.cn` 稀疏镜像加速；仓库未包含任何镜像配置，`Cargo.lock` 为官方源解析结果，
> 在普通网络环境直接 `cargo build` / `cargo test` 即可。

## 设计取舍与未实现项

- 单进程内存状态，无持久化、无鉴权、无水平扩展；`std::sync::Mutex` 内为纯内存同步操作，
  阻塞等待通过 `tokio::sync::Notify` 实现（注册发生在持锁期间，唤醒不丢失）。
- 区间端点为 `i64`；锁以“逐把申请、逐把精确释放”为粒度（同一事务可持有多把、可重叠）。
- 未实现意向锁、多粒度锁、死锁检测周期任务（改为入队即检测，对本服务规模更简单可靠）。
- 牺牲者策略固定为“环中最大事务 id”；未提供可插拔策略。
