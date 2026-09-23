# worksteal — 工作窃取执行器（纯后端）

一个用 Go 实现的**可测试后端调度库 + 本地 HTTP 接口**。核心是固定工作线程数的
工作窃取（work-stealing）任务执行器：每个工作线程有本地双端队列，空闲线程从
他人队列窃取任务；支持任务派生（spawn）与等待（wait），并通过
**help-the-child（帮助执行）** 从根本上避免“所有线程都在等待子任务”的
饥饿死锁。

调度时钟与执行器实现均可替换；所有状态变更产生结构化事件记录。**不含前端。**

---

## 1. 目录结构

```
.
├── go.mod
├── cmd/
│   ├── ws-server/        # 本地 HTTP 服务入口（JSON + SSE）
│   └── ws-acceptance/    # 命令行验收程序（深树/单线程/随机取消/可退出）
├── internal/
│   ├── clock/            # 可替换时钟：Real（墙钟）+ Fake（确定性手工推进）
│   ├── event/            # 结构化事件总线（有界环形缓冲、订阅、溢出策略）
│   ├── deque/            # 工作窃取双端队列：Chase-Lev 无锁 + Mutex 对照
│   ├── executor/         # 工作窃取执行器 + 可替换的 Direct 内联执行器
│   ├── scheduler/        # 延迟/周期调度（注入 Clock）
│   └── server/           # net/http 本地接口
├── examples/             # HTTP 请求样例
└── README.md
```

## 2. 核心设计

### 2.1 双端队列与窃取方向
- worker 自己从本地队列 **bottom 端 LIFO** 取任务（刚派生的子任务与父任务
  共享缓存、上下文，局部性好）。
- 空闲 worker 从受害者队列 **top 端 FIFO** 窃取（偷走最老、最独立的任务）。
- `ChaseLev`：push/pop/steal 走原子快速路径，仅在容量不足翻倍扩容时取独占锁；
  另有 `Mutex` 实现用于对照测试（`-deque mutex`）。

### 2.2 help-the-child：防饥饿死锁的关键
朴素实现里，父任务在 `Wait(子任务)` 时会阻塞底层线程。若所有 worker 都在
等待子任务，而子任务排在某个已被阻塞 worker 的队列里，就形成**饥饿死锁**，
单 worker 下必然发生。

本实现的 `Wait` **不阻塞线程**：等待中的 worker 继续从自己队列弹出任务、
从其他 worker 窃取任务并执行（helpJoin）。因此：

- **单 worker** 也能完成任意深度的递归树——等待子任务的那个 worker 会
  亲自把子任务跑掉；
- 任务的认领通过 `Queued -> Running` 的 CAS 完成，**一个任务恰好被一个
  worker 认领，任务体至多执行一次**。

### 2.3 任务生命周期与“至多一次”
```
queued ──CAS──> running ──┬──> completed
                         ├──> failed     (任务返回错误 / panic)
                         └──> canceled   (排队时取消 / 运行中 ctx 被取消)
queued ──────────────────────> canceled  (从未执行)
```
- 排队中取消：状态 CAS 为 canceled，弹出时直接跳过，**任务体零执行**。
- 运行中取消：取消其 context，并级联取消派生子孙；终态记账只发生一次。
- panic 被捕获包装为 `PanicError`，任务记为 failed，worker 不死亡。

### 2.4 可替换的时钟与执行器
- `clock.Clock`：生产用 `clock.Real`；测试用 `clock.Fake`，可手工 `Advance`
  并按计划时刻投递定时器，调度测试完全确定、无需真实睡眠。
- 执行器：`*executor.Executor`（工作窃取线程池）与 `*executor.Direct`
  （提交即在调用栈内联执行整棵树）实现同一组核心操作，调度器只依赖
  `Submit/Name/Bus` 这个最小接口。

### 2.5 结构化事件
每次执行器/任务状态变化都向 `event.Bus` 发布一条不可变 `Event`
（序号、时间戳、类型、执行器、任务、父任务、worker、错误等）。总线带
有界环形缓冲与订阅扇出，消费过慢按 `DropOldest/DropNewest` 丢弃，
**永不阻塞工作线程**。HTTP 层以 SSE 暴露实时事件流。

事件类型包括：`executor_started/stopping/stopped`、
`task_submitted/scheduled/spawned/stole/started/completed/failed/
canceled`、`worker_parked/woken` 等。

## 3. 构建与运行

要求 Go 1.22+（仅用标准库，无第三方依赖）。

```bash
go build ./...
# 启动 HTTP 服务（默认 127.0.0.1:8080，default 执行器 worker=NumCPU）
go run ./cmd/ws-server -addr 127.0.0.1:8080 -workers 4
```

也可使用 Makefile：`make build|run|test|test-race|acceptance|fmt|vet`。

## 4. 自动化测试

```bash
go test ./...                 # 全部单元/集成测试
go test -race ./...           # 竞态检测
go test ./... -count=3        # 重复运行查偶发
```

覆盖内容：

| 包 | 关键测试 |
|---|---|
| `deque` | LIFO/FIFO 顺序、扩容不丢元素、owner 与 thief 单元素竞争恰一个赢、20 万元素高并发“恰好取出一次”（`-race`） |
| `clock` | Fake 定时器顺序/同刻顺序/Stop/零与负时长、跨 goroutine 投递、Real |
| `executor` | **单 worker 深递归树（depth=12，8191 节点）**、多 worker 窃取、**随机取消（种子×worker∈{1,4}）核对至多一次+唯一终态+可退出**、排队取消绝不执行、取消级联、panic 隔离、立即关闭不跑排队任务 |
| `scheduler` | Fake 时钟下 After/At/Every/取消/关闭、与真实执行器端到端 |
| `event` | 订阅、历史快照、溢出丢弃、慢订阅者不阻塞、关闭 |
| `server` | 创建/提交/查询/取消/调度/优雅删除/SSE 事件流（httptest 端到端） |

### 验收程序

```bash
go run ./cmd/ws-acceptance -depth 10 -fanout 2 -workers 1
```

它显式核对三类场景并打印每项 PASS/FAIL、退出码反映结果：
- A：单 worker 深递归树（无取消），所有节点恰好一次、唯一终态、无死锁、可退出；
- B：单 worker 深树 + **随机取消内部任务**（排除根以得到“部分完成+部分取消”
  的混合树），核对至多一次、唯一终态、终态计数守恒、确有取消发生、可退出；
- C：4 worker + 随机取消，覆盖工作窃取路径。

可调参数：`-depth -fanout -workers -seed -cancel-every -work-ms`。

## 5. HTTP 接口

Content-Type 均为 `application/json`。成功创建任务返回 `202 Accepted`
与任务快照；事件流为 `text/event-stream`。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 健康检查 |
| GET | `/api/executors` | 列出执行器 |
| POST | `/api/executors` | 创建执行器 `{name,workers,deque}` |
| GET | `/api/executors/{name}` | 统计信息 |
| DELETE | `/api/executors/{name}?mode=graceful\|now` | 优雅/立即关闭并删除 |
| POST | `/api/executors/{name}/tasks` | 提交任务 |
| GET | `/api/executors/{name}/tasks` | 活跃 + 已终结任务 |
| GET | `/api/executors/{name}/tasks/{id}` | 单个任务状态 |
| POST | `/api/executors/{name}/tasks/{id}/cancel` | 取消任务 |
| POST | `/api/executors/{name}/schedules` | 延迟/周期调度 |
| DELETE | `/api/executors/{name}/schedules/{id}` | 撤销排程 |
| GET | `/api/executors/{name}/events` | SSE 结构化事件流 |

可提交的内建任务 `type`：

- `noop`：空任务（可带 `params.ms` 做可取消睡眠）；
- `sleep`：`params.ms` 可取消睡眠；
- `compute`：`params.iters` 次哈希运算（周期性检查取消）；
- `tree`：`params.{depth,fanout,work_ms}` 递归派生并等待的任务树，
  是 help-the-child 的直接演示。

具体请求/响应见 [`examples/http-requests.sh`](examples/http-requests.sh)
与 [`examples/schedule.json`](examples/schedule.json)。

## 6. 作为库使用（最小示例）

```go
ex := executor.New(executor.Config{Workers: 1})
_, _ = ex.Submit(func(ctx context.Context, c executor.Context) error {
    kids := make([]*executor.Task, 2)
    for i := range kids {
        t, err := c.Spawn(func(ctx context.Context, c executor.Context) error {
            // 子任务工作（会响应 ctx 取消）
            return nil
        })
        if err != nil {
            return err
        }
        kids[i] = t
    }
    // 等待期间当前 worker 会帮助执行其它任务（help-the-child），单 worker 也不死锁。
    for _, t := range kids {
        if err := c.Wait(t); err != nil {
            return err
        }
    }
    return nil
})
_ = ex.Shutdown(context.Background())
```
