# dynpool — 动态线程池缩容（纯后端）

Go 实现的**有界队列动态 worker（"线程"）池**：worker 数量可在运行中调整；缩容不打断
正在执行的任务；提供 4 种拒绝策略、优雅关闭与强制取消两套明确语义；所有状态变更以
**结构化事件**流输出；调度时钟（`Clock`）与 goroutine 启动器（`Executor`）均可替换，
便于确定性测试。池本身之上附带一个仅依赖标准库的本地 HTTP 接口（`cmd/dynpoold`）。

无第三方依赖，Go 1.22+。

## 目录

```
pool.go            核心类型：Pool/Config/Event/Handle/Status、拒绝策略常量
worker.go          worker 主循环、缩容排空（drain）逻辑、关闭期 drainer
submit.go          Submit、4 种拒绝策略、Resize
shutdown.go        Shutdown（优雅）/ ShutdownNow（强制）语义
status.go          Status 快照、Handle 查询
sinks.go           MemorySink / JSONSink / MultiSink
executor.go        TrackedExecutor（goroutine 计数，泄漏检测）
pool_test.go       验收测试（交错提交、缩容、关闭、阻塞任务）
policies_test.go   拒绝策略、事件、时钟替换、状态快照
internal/server/   HTTP 接口（stdlib net/http, Go 1.22 方法路由）
cmd/dynpoold/      HTTP 服务入口
cmd/leakcheck/     goroutine 泄漏检查小程序
examples/          curl 演示脚本与请求样例
RUNLOG.md          本机实际运行记录（命令与结果）
```

## 语义定义（验收口径）

### 缩容 Resize(n)

- 扩容：立即补足到 n 个 worker。
- 缩容：选中的 worker 立即进入 **retiring**（事件 `worker.retiring`，状态计数
  `retiring_workers` 立刻可见），但**正在运行的任务不会被打断**；任务结束后该 worker
  会先排空队列里的剩余任务，队列为空才退出（`worker.exited`）。
- 因此缩容到任何值（含 0）都**不会丢失已接收任务**：其它在役 worker 或退休中的 worker
  会把队列排空；若关闭时在役 worker 已为 0 而队列非空，内部临时 `drainer` 兜底。

### 有界队列与拒绝策略

队列满时 `Submit` 按 `reject_policy` 处理：

| 策略 | 行为 | Submit 返回 |
|---|---|---|
| `abort`（默认） | 拒绝，任务不执行，发 `task.rejected` | `ErrTaskRejected`（HTTP 429） |
| `discard` | 静默丢弃，任务不执行，发 `task.rejected` | `nil`，handle 已结束 |
| `discard_oldest` | 丢弃队首旧任务（记 rejected），新任务入队 | 通常 `nil` |
| `caller_run` | 在提交者 goroutine 上同步执行（算已接收、正常完成） | 任务错误 |

### 关闭

- **优雅 `Shutdown(ctx)`**：拒绝新提交（`ErrPoolShuttingDown`）；所有已接收任务（运行中
  与排队中）带着**未取消的 context** 全部执行完；之后所有 worker 退出，池变 `stopped`。
  支持 `GracePeriod`（超时只让调用者提前返回，后台仍继续排空，最终到 `stopped`）。幂等。
- **强制 `ShutdownNow()`**：立即取消所有运行中任务的 context（协作式阻塞任务应尽快返回
  `context.Canceled`）；把队列中尚未执行的任务 id 取出返回，这些任务**不会执行**
  （`task.dropped`，handle 标记为未运行结束）；等待 worker 全部退出。幂等。

### 任务执行保证

- 每个**已接收**任务恰好执行一次（`task.completed` 恰好一次）；被拒绝/被强制丢弃的任务
  零次执行。
- 任务函数签名 `func(ctx context.Context) error`；强制关闭时 ctx 取消。
- `Handle` 可等待、查询是否真的运行过（`Result() (ran bool, err error)`）。

### 事件

`Event{Seq, Time, Type, Pool, Worker, TaskID, OldSize, NewSize, Reason, Detail}`，
`Seq` 单调递增。类型见 `pool.go` 中的 `Event*` 常量（池/worker/任务/resize 四类）。
可接 `MemorySink`、`JSONSink`（JSON Lines）、`MultiSink` 或自定义 `Sink`。
约定：`Sink.OnEvent` 不得回调同一个池。

## HTTP 接口

`go run ./cmd/dynpoold -addr 127.0.0.1:8080`

| 方法与路径 | 说明 |
|---|---|
| `POST /v1/pools` | 创建命名池 |
| `GET /v1/pools` | 列出全部池状态 |
| `GET /v1/pools/{name}` | 池状态快照 |
| `DELETE /v1/pools/{name}` | 优雅关闭；`?force=1` 强制 |
| `POST /v1/pools/{name}/workers` | 调整 worker 数 `{"workers":n}` |
| `POST /v1/pools/{name}/tasks` | 提交任务（见下） |
| `GET /v1/pools/{name}/tasks` | 已接收任务 id 列表 |
| `GET /v1/pools/{name}/tasks/{id}` | 任务结果（ran/error/result） |
| `GET /v1/pools/{name}/events?from=&type=` | 结构化事件查询 |
| `POST /v1/blocks/{name}/release?name=X` | 释放命名阻塞任务 |

任务类型（演示用，便于通过 HTTP 制造阻塞）：
- `{"type":"sleep","sleep_ms":50}`
- `{"type":"block","id":"b1","name":"b1"}`（一直阻塞，直到 release）
- `{"type":"echo","payload":"hello"}`

完整 curl 样例见 **[examples/http-samples.md](examples/http-samples.md)**，
一键演示脚本：**[examples/demo.sh](examples/demo.sh)**。

## 库的基本用法

```go
sink := &dynpool.MemorySink{}
p, err := dynpool.New(dynpool.Config{
    Name: "w1", Workers: 4, QueueSize: 128,
    Reject: dynpool.PolicyAbort, Sink: sink,
})
h, err := p.Submit(func(ctx context.Context) error {
    // ... do work; ctx canceled only by ShutdownNow
    return nil
})
ran, err := h.Result()           // 等待完成
_ = p.Resize(2)                  // 运行中缩容，当前任务不受影响
_ = p.Shutdown(context.Background()) // 优雅：排空后停止
// dropped := p.ShutdownNow()    // 或强制：取消运行中、返回排队任务 id
```

## 构建与测试

```bash
go build ./...
go vet ./...
go test -race -count=1 ./...          # 全部单元 + HTTP 测试（竞态检测）
go run ./cmd/leakcheck                # goroutine 泄漏检查（20 轮建/缩/关）
go run -race ./cmd/leakcheck
```

测试覆盖（`pool_test.go` / `policies_test.go` / `internal/server/server_test.go`，
共 26 个测试）：

- `TestInterleavedSubmitResizeShutdown`：8 个提交者 ×100 任务与反复 resize、shutdown
  交错，断言 已接收数 == 执行数、每任务恰好一次、无 goroutine 泄漏。
- `TestShrinkWaitsForRunningTask`：3 个阻塞任务运行中缩容 3→1，retiring 立即为 2、
  active 保持 3 直到释放，之后稳定在 1。
- `TestShrinkDoesNotLoseQueuedTasks`：4 worker 全阻塞 + 20 排队任务，缩容到 0 再优雅
  关闭，20 个任务全部执行且无重复。
- `TestSubmitShutdownRaceNoStrandedTask`：0 worker 时 Submit 与 Shutdown 高频交错
  （50 轮），保证无任务滞留。
- `TestShutdownNowDropRace`：取消瞬间 worker 与排空方争抢队列任务（100 轮），
  dropped+ran 守恒、无重复记账。
- `TestGracefulThenForceEscalation`：优雅关闭进行中升级为强制的返回值与终止事件。
- `TestGracefulShutdownDrainsQueued` / `TestGracefulShutdownZeroWorkers`（drainer 路径）。
- `TestShutdownNowCancelsAndDrops`：运行中任务收到 `context.Canceled`，6 个排队任务被
  返回且不执行，计数正确，幂等，关闭后提交/resize 报 `ErrPoolStopped`。
- 四种拒绝策略、重复/非法 id、事件顺序与字段、可替换时钟、HTTP 全链路。

推荐的验证命令：

```bash
go test -race -count=10 ./...   # 反复跑以暴露调度竞态
```

## 设计说明

- worker 间无任务转交（无 work-stealing）：缩容只靠"退休 worker 排空队列 + 在役 worker
  正常消费"保证不丢任务，交互最少、可证明。
- 退休信号是 per-worker 的 `chan struct{}`；关闭用 `context.CancelFunc` 表示强制取消，
  优雅关闭**不**取消该 context。
- `taskWG` 跟踪所有已接收任务（含 caller_run 同步任务），优雅关闭等待
  `taskWG + worker wg` 双归零。
- `Executor` 抽象让测试用 `TrackedExecutor` 精确计数 goroutine；`Clock` 抽象让事件时间
  戳可替换（生产默认 wall clock）。
- HTTP 层无持久化、无鉴权、默认仅监听 127.0.0.1，定位为本地演示/验收接口，不是生产服务。
