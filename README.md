# lockmgr — 单机两阶段锁与等待图死锁检测

纯后端、单进程内实现的事务锁管理器。提供共享锁（S）/排他锁（X）、S→X 锁升级、
FIFO 等待队列、按事务 ID 排序的等待图（wait-for graph）死锁检测、确定性可解释的
牺牲者选择、锁等待超时、事务中止/提交后批量释放（无锁泄漏），以及等待图边数上限
保护与告警指标。HTTP 接口基于 [Axum](https://github.com/tokio-rs/axum)。

## 依赖

- Rust（实测 1.98.1，edition 2021；理论上 ≥1.75 即可）、Cargo
- 无外部数据库/锁服务/中间件，仅单进程内存状态
- Rust 依赖（已锁定在 `Cargo.lock`）：`axum 0.8`、`tokio 1`（full）、`serde 1`、`serde_json 1`；
  测试用 `reqwest 0.12`（rustls）

## 构建与启动

```bash
cargo build --release
./target/release/lockmgr                 # 默认监听 0.0.0.0:3000

# 可选环境变量
PORT=3000 \
LOCK_BUCKETS=64 \
MAX_WAIT_EDGES=10000 \
LOCK_TIMEOUT_MS=5000 \
  ./target/release/lockmgr
```

开发模式直接 `cargo run`。绑定成功后才会打印 `listening` 日志。

| 环境变量 | 默认值 | 含义 |
| --- | --- | --- |
| `PORT` | 3000 | HTTP 监听端口 |
| `LOCK_BUCKETS` | 64 | 锁表哈希桶数（资源名 `DefaultHasher` 分桶） |
| `MAX_WAIT_EDGES` | 10000 | 等待图边数上限，达到上限拒绝新事务与新等待请求 |
| `LOCK_TIMEOUT_MS` | 5000 | 锁等待默认超时（每次请求也可单独指定 `timeout_ms`） |

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/tx` | 开启事务，返回 `{"tx_id": N}` |
| POST | `/tx/{id}/locks` | 申请锁，body 见下；阻塞至授予/死锁中止/超时 |
| POST | `/tx/{id}/commit` | 提交，批量释放全部锁 |
| POST | `/tx/{id}/abort` | 主动中止，批量释放全部锁 |
| GET  | `/tx/{id}` | 查询事务状态 |
| GET  | `/graph` | 当前等待图边集（`{from,to}` 列表，按 ID 排序） |
| GET  | `/metrics` | 全部统计指标（JSON） |
| GET  | `/report` | 人类可读的统计报告（纯文本） |

申请锁请求体：

```json
{ "resource": "A", "mode": "exclusive", "timeout_ms": 5000 }
```

- `mode`：`"shared"` 或 `"exclusive"`；已持 S 再请 X 为升级请求。
- `timeout_ms` 可省略，缺省用服务默认值。

错误响应统一为 `{"error": <code>, "detail": <string>}`：

| HTTP | error code | 场景 |
| --- | --- | --- |
| 409 | `deadlock_victim` | 本事务被选为死锁牺牲者，detail 含环路径与选择理由 |
| 408 | `lock_timeout` | 等待超时，事务已被整体中止（批量释放） |
| 409 | `aborted` | 等待期间事务被中止 |
| 409 | `already_waiting` | 同一事务已有一个未决锁请求 |
| 410 | `tx_not_active` | 事务已提交/已中止 |
| 404 | `tx_not_found` | 事务不存在 |
| 503 | `wait_graph_full` | 等待图边数达上限，拒绝新事务/新等待 |

## 请求样例

一键演示（需先启动服务）：

```bash
bash scripts/demo.sh                      # BASE 可覆盖，如 BASE=http://127.0.0.1:8080
```

手动执行：

```bash
B=http://127.0.0.1:3000

# 1) 两个事务各持一把 X 锁
T1=$(curl -s -X POST $B/tx | python3 -c 'import sys,json;print(json.load(sys.stdin)["tx_id"])')
T2=$(curl -s -X POST $B/tx | python3 -c 'import sys,json;print(json.load(sys.stdin)["tx_id"])')
curl -s -X POST $B/tx/$T1/locks -H 'content-type: application/json' \
  -d '{"resource":"A","mode":"exclusive"}'
curl -s -X POST $B/tx/$T2/locks -H 'content-type: application/json' \
  -d '{"resource":"B","mode":"exclusive"}'

# 2) T1 请求 B（挂起等待，边 T1 -> T2）
curl -s -X POST $B/tx/$T1/locks -H 'content-type: application/json' \
  -d '{"resource":"B","mode":"exclusive","timeout_ms":10000}' &

# 3) T2 请求 A，环闭合；T2 立即收到 409 牺牲者响应，T1 在后台被授予 B
curl -s -X POST $B/tx/$T2/locks -H 'content-type: application/json' \
  -d '{"resource":"A","mode":"exclusive","timeout_ms":10000}'

# 4) T1 提交后，重放被中止的事务
curl -s -X POST $B/tx/$T1/commit
T3=$(curl -s -X POST $B/tx | python3 -c 'import sys,json;print(json.load(sys.stdin)["tx_id"])')
curl -s -X POST $B/tx/$T3/locks -H 'content-type: application/json' -d '{"resource":"A","mode":"exclusive"}'
curl -s -X POST $B/tx/$T3/locks -H 'content-type: application/json' -d '{"resource":"B","mode":"exclusive"}'
curl -s -X POST $B/tx/$T3/commit

# 5) 报告 / 指标 / 等待图
curl -s $B/report
curl -s $B/metrics
curl -s $B/graph
```

## 设计说明

### 锁与队列

- 锁表为 `buckets: Vec<HashMap<resource, LockEntry>>`，资源名哈希取模分桶；
  每个 `LockEntry` 记录持有者集合（tx→模式）与 FIFO 等待队列。
- 兼容性：S 与 S 兼容；X 与任何模式冲突。新请求立即可授予才入锁表，否则排队。
- S→X 升级：自己是唯一持有者时就地升级；否则升级请求插入队首并等待。
- 每次释放都 pump 队列：队首请求可满足则授予并继续，直到队首不可满足为止；
  空锁条目即时删除，避免内存增长。

### 等待图与死锁检测

- 等待边 `waiter -> blocker`，邻接集合用 `BTreeSet`（按事务 ID 升序遍历），
  阻塞源包括两类：
  1. 与请求模式冲突的当前**持有者**；
  2. FIFO 队列中**排在前面的冲突等待者**——后到请求不可能越过队首获得锁。
  第 2 类边不可或缺：存在只经队列闭合的环（例：S 持有者 + 排队的 X 升级者 +
  其后排队的 S 请求，见测试 `cycle_closed_through_waiter_queue_is_detected`），
  若只画持有者边会漏检、只能靠超时兜底（误杀）。
- **检测时机**：每次有新等待边加入后，从该等待者出发做 DFS；发现回到起点的环即死锁。
  因此检测延迟就是一次图遍历时间（微秒级，验收断言 < 2s），无后台轮询。
- **牺牲者选择（确定性、可解释）**：取环上事务 ID 最大者（= 最晚开始、最年轻的事务）。
  同样的图状态永远得到同一结果；响应 `detail` 与 `/metrics` 的 `deadlock_log`
  都会记录形如 `cycle T3 -> T2 -> T3; victim T3 (largest txid = youngest ...)` 的解释。
- 牺牲者被整体中止、批量释放全部锁并唤醒环上其他事务，因此一次检测**恰好中止一个事务**。

### 超时与误杀

- 等待可设超时（请求级 `timeout_ms` 或服务默认）。超时后事务被**整体中止**，
  其持有锁与等待请求全部清理（无锁泄漏）。
- 指标区分：
  - `timeout_aborts`：超时中止总数；
  - `timeout_aborts_false_kill`：超时时**不在任何等待环上**（未确认死锁即被杀）；
  - `timeout_aborts_in_cycle`：超时时仍在环上（即时检测漏判的兜底告警，正常恒为 0）。

### 容量保护

- 等待图边数硬上限（默认 10000）。新等待请求按其将新增的边数做容量预检，
  超限返回 503；边数已达上限时 `POST /tx` 也返回 503。
- 每次拒绝都：累加 `wait_graph_full_rejections` 指标，并向 stderr 输出 `[ALERT]` 行。
- 图边随授予/中止/提交即时增减，容量恢复后服务自动恢复。

### 无锁泄漏保证

事务的每条锁都同时登记在 `tx_locks: tx -> {resources}`；提交、用户中止、
死锁中止、超时中止四条结束路径统一走批量释放 + 队列 pump + 图边清理，
`/metrics` 的 `current_holders / current_waiters / wait_graph_edges` 在全部
事务结束后必须归零（验收测试 `assert_no_leak` 强制校验）。

并发模型：单个 `std::sync::Mutex<Inner>` 保护全部状态，临界区内只做内存操作，
跨任务唤醒通过 `tokio::sync::oneshot` 在锁外发送，持锁期间不 `.await`，
因此不会跨 `.await` 持锁，也不会有通知死锁。

## 自动化测试

```bash
cargo test           # 单元测试(8) + HTTP 验收测试(8)
cargo clippy --all-targets
```

- 单元测试（`src/lock_manager.rs`，直接打锁管理器）：
  S/S 兼容、X 阻塞与授予、就地升级、两事务环只中止一个且牺牲者确定、
  升级死锁、超时中止与无泄漏、图容量拒绝、**经队列闭合的环检测（零误杀）**。
- HTTP 验收测试（`tests/acceptance.rs`，随机端口起真实 Axum 服务）：
  - `three_conflict_types`：并发注入 S/X、X/X、X/S 三类冲突（S/S 兼容对照）；
  - `two_tx_cycle_detection_and_replay`：环状等待，<2s 内检测、只中止一个、
    幸存者被授予、被中止事务重放可完成；
  - `three_tx_cycle`：T1→T2→T3→T1 三事务环，牺牲者为最年轻者，其余顺序完成；
  - `lock_upgrade_and_upgrade_deadlock`：就地升级与升级死锁；
  - `queue_mediated_cycle_detected_without_false_kill`：队列边环 + 零误杀 + 重放；
  - `lock_timeout_counts_as_false_kill`：超时计误杀、死后事务操作返回 410；
  - `wait_graph_cap_rejects_and_alerts`：上限拒绝新事务（503）与告警指标，
    图清空后恢复；
  - `report_endpoint_outputs_stats`：报告含等待链长度与误杀统计。

## 实测结果（2026-09-25，Rust 1.98.1，Linux x86_64）

- `cargo test`：**16/16 通过**（8 单元 + 8 HTTP 验收），用时约 0.7s；`cargo clippy` 无告警。
- `scripts/demo.sh` 实跑：环 `T3 -> T2 -> T3` 即时检出，仅 T3 收到 409
  （detail 含 `victim T3 (largest txid ...)`），T1 被授予 B 后正常提交，
  重放事务 T4 成功提交，最终 `holders=0 waiters=0 edges=0`。
- 容量实测（`MAX_WAIT_EDGES=2`）：2 条边时 `POST /tx` 返回 503 与
  `wait_graph_full`，stderr 出现 `[ALERT] wait-for graph full ...`，
  持有者提交、图清空后新事务恢复 200；`wait_graph_full_rejections=1`。
- 报告字段：等待链长度（samples/avg/max）、死锁检出与中止数、误杀数、
  容量拒绝数均按预期累计。

## 未完成项 / 已知边界

- 仅单进程内存实现（需求约束）：重启状态丢失，无持久化、无多实例协调。
- 无鉴权、无资源命名空间隔离；资源名为任意字符串。
- 状态用单 Mutex 串行化，面向功能正确性而非超高吞吐；分桶结构已具备，
  如需更高并发可逐桶加锁（当前规模/需求下无必要）。
- 等待链长度统计在"新增等待边"时采样（从该等待者出发的最长 DFS 路径），
  不是持续拓扑深度的精确分布。
- 锁授予为严格 FIFO 策略，未做读写锁偏向（防饿死）调优；饥饿最终由超时中止兜底。
