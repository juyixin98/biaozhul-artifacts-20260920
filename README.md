# dynpool — 动态线程池缩容（纯后端）

一个用 Go 实现的、**可在运行时调整线程数**的有界队列工作线程池，附带本地 HTTP
接口。重点解决：缩容时**等待在执行任务安全结束**、拒绝策略语义清晰、优雅关闭与
强制取消语义明确、所有状态变更产出**结构化事件**。调度时钟（Clock）与执行器
（Executor）均可替换，因此并发行为可以被确定性地测试。

无前端、无第三方依赖，仅用标准库。

---

## 目录结构

```
.
├── go.mod
├── pool/                 # 核心调度库（无任何 I/O 依赖）
│   ├── clock.go          # Clock/Timer 抽象：RealClock + FakeClock（可手动推进）
│   ├── events.go         # Event/Sink：结构化事件、环形记录器、订阅、JSON 落盘
│   ├── task.go           # Task/Future/State/Executor、哨兵错误
│   ├── queue.go          # 定容环形 FIFO 队列
│   ├── ctx.go            # 用 forceCh 适配出的可取消 context
│   ├── pool.go           # 线程池主体：提交/调度/扩缩容/关闭
│   └── pool_test.go      # 库的并发与语义测试（-race 通过）
├── server/               # 本地 HTTP 接口
│   ├── server.go
│   ├── demo.go           # 内置可阻塞/可失败的演示任务
│   └── server_test.go
├── cmd/dynpoold/main.go  # 服务入口（信号优雅退出）
├── examples/requests.sh  # 端到端 curl 请求样例
└── README.md
```

---

## 快速开始

需要 Go 1.22+。

```bash
# 运行测试（含竞态检测）
go test ./... -race

# 启动服务
go run ./cmd/dynpoold -addr 127.0.0.1:8080 -workers 4 -queue 8 -policy abort

# 另开一个终端，跑请求样例
./examples/requests.sh
# 强制取消路径：
FORCE=1 ./examples/requests.sh
```

构建二进制：

```bash
go build -o bin/dynpoold ./cmd/dynpoold
./bin/dynpoold -workers 8 -queue 256 -policy caller_runs
```

---

## 语义说明（这是本项目的核心契约）

### 1. 有界队列与拒绝策略

队列容量同时计入「已排队」与「已定时未到点」的任务。队列满时，按 `Policy`：

| 策略 | 行为 | 返回 |
|------|------|------|
| `abort`（默认） | 拒绝本次提交，调用方保留任务 | `ErrQueueFull` |
| `caller_runs` | 在 **Submit 调用方自己的 goroutine** 同步执行，形成背压；任务仍只结算一次 | `nil` |
| `discard` | 静默丢弃**最新**（当前）任务 | `ErrTaskDiscarded` |
| `discard_oldest` | 驱逐**最旧**排队任务（结算为 canceled），再入队当前任务 | `nil` |

被拒绝的提交也会得到一个已结算（`rejected`）的 Future 并发出 `task.rejected` 事件。

### 2. 运行时扩缩容

- `Resize(n)`：扩容立即 spawn；缩容只下发「退役通知」(`retiring`)。
- **缩容绝不打断正在执行的任务**。worker 只有在「尝试取任务失败、确认自己空闲」
  时才会认领一条退役通知并退出；正在跑任务的 worker 一定先把任务跑完。
- 缩容与扩容可交错：先 `Resize(1)` 再 `Resize(5)`，尚未被认领的退役通知会被直接
  撤销（reclaim），只有已真正退出的名额需要补 spawn。最终服务容量精确收敛到目标值。
- worker 的退役（`retiring--` 与 `active--`）在**同一个临界区**完成，避免缩/扩
  交错时把「正在退出的 worker」误算为在役而少 spawn。

### 3. 优雅关闭 `Shutdown(ctx)`

- 关闭开始后拒绝新提交（`ErrPoolShuttingDown`）；
- 已定时未到点的任务立即入队；
- worker 把队列里**所有已接收任务全部执行完**才退出（drain）；
- 等待全部终止；`ctx` 超时返回 `ErrShutdownTimeout`，但排空在后台继续，可再用
  `AwaitTermination` 等待。
- 即使关闭前已 `Resize(0)`（没有 live worker），内部 `drainer` 也会保证已接收任务
  不丢失。

### 4. 强制取消 `ShutdownNow(ctx)`

- 拒绝新提交；
- 立即取消**所有正在运行任务的 context**（协作式：任务需要观察 `ctx.Done()` 提前
  返回；忽略 ctx 的任务仍会跑完——本库**不会在中途强杀任务**，以免破坏任务自身状态）；
- 队列中、以及定时未到点、从未开始的任务**不再执行**，统一结算为 `canceled`，其
  快照通过返回值 `notRun` 给出；
- worker 在当前任务一返回后立即退出；
- 返回 `([]TaskSnapshot, error)`，等待 worker 退出或 `ctx` 超时。

> 已开始执行的任务即使因 ctx 取消而返回错误，也结算为 `failed`（它确实跑过）；只有
> **从未开始**的任务才是 `canceled`。

### 5. 恰好一次（exactly-once）

- 每个被**接收**的任务最终恰好结算一次：`completed` / `failed` / `canceled`；
- panic 会被 recover 并记为 `failed`（`ErrTaskPanicked`），**不会杀死 worker**；
- Future 内部有单向状态机 + `done` channel，重复结算被防御性拦截。

---

## HTTP API

所有请求/响应均为 JSON。

| 方法 & 路径 | 说明 |
|---|---|
| `GET  /healthz` | 存活探针 |
| `GET  /v1/pool` | 返回 `Stats`（线程数、队列、各项计数、状态） |
| `POST /v1/pool/resize` | body `{"workers": N}`，扩缩容 |
| `POST /v1/pool/shutdown` | 优雅关闭；`?force=1` 强制取消；`&timeout_ms=N` 限等待 |
| `GET  /v1/tasks` | 所有任务快照 |
| `POST /v1/tasks` | 提交内置演示任务 |
| `GET  /v1/tasks/{id}` | 单个任务快照 |
| `GET  /v1/events` | 最近保留的结构化事件 |

提交任务 body（内置 demo 任务，便于压测阻塞/取消）：

```json
{ "id": "t1", "type": "demo", "sleep_ms": 200, "fail": false }
```

- `sleep_ms`：阻塞时长，会响应强制取消（select `ctx.Done()`）。
- `fail`：是否返回错误。
- `id` 可省略（自动生成）。

状态码：`202` 已接收；`400` 参数错；`404` 任务不存在；`409` 关闭中/已终止仍提交；
`503` 队列满被拒（abort/discard）；`408/503` 关闭等待超时。

### Stats 示例

```json
{
  "name": "default",
  "state": "running",
  "desired_workers": 4,
  "active_workers": 4,
  "retiring_workers": 0,
  "queue_len": 0,
  "queue_cap": 8,
  "scheduled": 0,
  "accepted": 12, "rejected": 1, "started": 12,
  "completed": 11, "failed": 1, "canceled": 0,
  "running_now": 0
}
```

---

## 结构化事件

`pool.Sink` 接口接收不可变 `Event`。内置实现：

- `NopSink`：丢弃；
- `NewEventRecorder(n)`：保留最新 n 条环形记录，支持 `Subscribe` 实时订阅（慢订阅者
  丢事件而不阻塞池）；
- `NewJSONSink(w)`：每行一个 JSON（NDJSON）落盘；
- `MultiSink(...)`：扇出多路。

事件类型（`Event.Kind`）：

```
pool.created / pool.resizing / pool.shutdown / pool.force_canceled / pool.terminated
worker.started / worker.stopped
task.submitted / task.scheduled / task.started
task.completed / task.failed / task.canceled / task.rejected
```

每条事件带时间、池名、worker/task id、当前/目标线程数、队列长度、累计完成数、原因与
错误串。测试用它来验证「无重复完成」「缩容退出原因」等。

---

## 可替换点（利于测试）

- `Config.Clock`：默认 `RealClock`；测试注入 `FakeClock`，`Advance(d)` 手动推进时间，
  定时任务的触发完全确定（见 `TestScheduleWithFakeClock`）。
- `Config.Executor`：默认 `DirectExecutor`（在 worker goroutine 直接调用）；可包裹
  追踪/注入延迟/故障。
- `Config.Sink`：事件出口。

### 作为库使用

```go
import "dynpool/pool"

p, _ := pool.New(pool.Config{
    Name: "jobs", Workers: 4, QueueCapacity: 128,
    Policy: pool.RejectCallerRuns,
    Sink:   pool.NewEventRecorder(1024),
})

f, err := p.Submit(pool.Task{
    Type: "resize-image",
    Fn: func(ctx context.Context) (any, error) {
        // ctx 在 ShutdownNow 时会被取消
        return doWork(ctx)
    },
})
res, err := f.Get(ctx)   // 阻塞到结算

_ = p.Resize(2)          // 缩容，正在执行的任务不受影响
_ = p.Shutdown(ctx)      // 优雅：排空已接收任务
// 或：notRun, err := p.ShutdownNow(ctx)
```

---

## 验收点如何被覆盖

`pool/pool_test.go` 中的关键测试：

| 验收要求 | 测试 |
|---|---|
| 交错提交/缩容/关闭 | `TestInterleavedSubmitResizeShrinkShutdown`（8 生产者 ×150 任务 + 持续 resize churn + 优雅关闭） |
| 缩容等待任务安全结束 | `TestShrinkWaitsForRunningTasks`（8 个阻塞任务，缩容 4→1 期间 `running` 不允许下降） |
| 无丢失已接收任务 | `TestGracefulShutdownDrainsAllAccepted`、`TestGracefulDrainAfterResizeZero`（Resize(0) 后仍排空）、交错测试统计 |
| 无重复完成 | `TestNoDuplicateCompletion` + 交错测试按 Future 与事件双重计数（每个任务恰好 1 个终态事件） |
| 无线程泄漏 | `TestNoGoroutineLeak`（多轮创建/resize/关闭后 goroutine 数回落） |
| 拒绝策略四种 | `TestRejectPolicies/{abort,discard,discard_oldest,caller_runs}` |
| 优雅 vs 强制语义 | `TestShutdownNowCancelsPendingNotRunning`、`TestShutdownNowRunningTaskIgnoresContext`、`TestShutdownTimeoutDoesNotLoseTasks` |
| panic 不杀 worker | `TestPanicTaskDoesNotKillWorker` |
| 时钟可替换 | `TestScheduleWithFakeClock`、`TestScheduleCapacityCountsScheduled` |
| 扩缩容收敛 | `TestGrowThenShrinkCoalescing` |
| HTTP 端到端 | `server/server_test.go` |

运行：

```bash
go test ./... -race -count=10     # 重复跑以暴露时序问题
go test ./... -cover              # 覆盖率
go vet ./...
```

---

## 设计要点（实现备忘）

- 唤醒机制：每个 worker 持有一个容量 1 的信号槽，park 前在池锁内登记到 `parked`
  map；提交/扩缩容/关闭在锁内非阻塞 `wakeAllLocked()`。由于「状态检查」与「槽登记」
  在同一把锁内，信号不可能落在检查与阻塞之间而丢失（比手写 channel 替换广播更稳）。
- 缩容用整数计数 `retiring` 而不是向 channel 投递 token，扩容时可直接做减法撤销；
  退出与计数在同一临界区，避免缩/扩交错少 spawn。
- 强制取消用一个共享 `forceCh` 适配成每任务 `context.Context`（`ctx.go`），无需为每个
  任务分配 cancel 函数。
- 所有事件在**释放池锁之后**再 emit / 触发 Future 回调，回调 panic 被 recover，用户
  回调不会反向持有池内部锁（避免锁序倒置）。
