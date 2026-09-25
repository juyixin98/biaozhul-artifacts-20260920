# worksteal — 工作窃取执行器（纯后端）

一个用 Go 实现的**固定工作线程工作窃取任务执行器**库，外加一个本地 HTTP 接口。
重点特性：

- 每个 worker 拥有本地**双端队列（deque）**：worker 自己从底部 LIFO 取任务，
  空闲 worker 从随机受害者队列**顶部窃取一半**（Chase–Lev 风格，互斥锁实现）。
- 任务可以**派生子任务并等待**（spawn/wait）。worker 在等待期间不会阻塞，
  而是**帮忙执行其它任务**（包括它等待的子任务）——因此
  **单线程模式（workers=1）也能运行任意深的 spawn/wait 任务树，不会发生线程饥饿死锁**。
- **调度时钟可替换**（`scheduler.Clock`）：真实时钟或手动推进的 `MockClock`，
  定时相关逻辑可确定性测试。
- **每次状态变更都发出结构化事件**（`scheduler.EventSink`）：JSONL 文件、
  内存环形缓冲、通道等；事件携带全局单调递增的 `seq`，投递顺序与 seq 一致。
- 支持**取消传播**（取消一个任务即取消其整个后代子树）、任务失败与 panic 隔离、
  优雅关闭（pending 任务立即取消、running 任务自然跑完；超时后协作式取消）。
- 保证**每个任务的函数至多执行一次**，且每个任务恰好到达一个终态。

无前端，仅标准库依赖。

## 目录结构

```
scheduler/   核心调度库
  clock.go     Clock/RealClock/MockClock（可替换时钟、可停定时器）
  deque.go     每 worker 的本地双端队列 + 窃取
  events.go    事件类型、EventSink（Nop/Channel/JSON/Memory）
  executor.go  固定 worker 池、提交、取消、关闭、统计、事件
  runtime.go   任务运行时：Spawn/SpawnFunc/Wait/Sleep（等待时帮忙执行）
commands/    HTTP 可提交的内置任务类型（recurse/fanout/chain/sleep/fail/panic/noop）
server/      net/http 本地接口
cmd/wsd/     可运行服务
```

## 快速开始

需要 Go 1.22+。

```bash
go build -o bin/wsd ./cmd/wsd
./bin/wsd -addr 127.0.0.1:8080 -workers 4 -event-log events.jsonl
```

参数：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `-addr` | `127.0.0.1:8080` | 监听地址 |
| `-workers` | `NumCPU` | 固定 worker 数（≥1；**1 即单线程模式**） |
| `-event-log` | 空 | 可选：把全部事件以 JSON Lines 追加写入该文件 |
| `-event-buffer` | `4096` | 内存事件环形缓冲大小（支撑 `GET /events`） |
| `-drain-timeout` | `10s` | 优雅关闭时等待运行任务的上限，超时后取消任务 ctx |

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/healthz` | 健康检查；关闭中返回 503 |
| `GET` | `/stats` | 执行器计数快照 |
| `POST` | `/tasks` | 提交一个根任务 |
| `GET` | `/tasks?limit=N` | 列出任务快照 |
| `GET` | `/tasks/{id}` | 查询单个任务 |
| `POST` | `/tasks/{id}/cancel` | 取消任务及其整个子树 |
| `GET` | `/events?after_seq=N` | 读取结构化事件（内存环形缓冲） |
| `POST` | `/shutdown` | 开始排空并关闭执行器 |

提交体：`{"kind": "<类型>", "payload": { ... }}`。

### 内置任务类型

| kind | payload | 行为 |
| --- | --- | --- |
| `recurse` | `{"depth": D}` | 派生 2 个 depth-1 并等待；结果为子树节点数 `2^(D+1)-1`。**单线程饥饿死锁探针** |
| `fanout` | `{"count": N, "child_kind": "noop"\|"recurse"\|"sleep", ...}` | 派生 N 个叶子并等待 |
| `chain` | `{"count": N}` | 顺序派生并等待的 N 级链 |
| `sleep` | `{"millis": M}` | 睡眠 M 毫秒（时钟感知） |
| `fail` | `{"message": "..."}` | 返回错误 |
| `panic` | `{"message": "..."}` | panic（被隔离，记为 `panicked`） |
| `noop` | `{}` | 立即成功 |

任务状态：`pending` → `running` → `succeeded | failed | panicked | canceled`。

可直接运行的请求样例见 [`examples/requests.md`](examples/requests.md)，
或执行验收脚本 `scripts/acceptance.sh`。

## 作为库使用

```go
import "worksteal/scheduler"

e, _ := scheduler.New(
    scheduler.WithWorkers(1),                 // 单线程也不会死锁
    scheduler.WithSinks(scheduler.NewJSONSink(logFile)),
)
defer e.Shutdown(context.Background())

h, _ := e.SubmitFunc("my-task", func(ctx context.Context, rt scheduler.Runtime) (any, error) {
    child, _ := rt.SpawnFunc(ctx, "child", func(ctx context.Context, rt scheduler.Runtime) (any, error) {
        return 1, nil
    })
    v, err := rt.Wait(ctx, child)             // 等待时本 worker 继续帮忙干活
    return v, err
})
res, _ := e.Result(context.Background(), h)
```

## 设计要点：为什么单线程不会饿死

朴素线程池里，任务 A 派生子任务 B 后阻塞等待 B：占着线程的 A 睡眠，B 在队列里
永远轮不到线程执行 → 饥饿死锁。本执行器中，`Wait`/`Sleep` **不占用一个等待线程**：
当前 worker 一边等待，一边继续从（任意 worker 的）队列取任务执行。因此：

- workers=1 时：根任务等待 → 唯一的 worker 亲自执行子任务 → 子任务完成 → 根任务继续；
- 任意深度的二叉 spawn/wait 树都能在一个 worker 上跑完（见验收测试与实际运行记录）。

等待采用标准的“条件变量 + 谓词在锁内检查”模式，定时器/ctx 事件桥接为广播，
唤醒不会丢失。

## 结构化事件

每次状态变化产生一条 `scheduler.Event`（JSON 示例）：

```json
{"seq":42,"time":"2026-09-23T22:33:08.995+08:00","type":"task.completed",
 "task_id":"t-1","parent_id":"","kind":"recurse","state":"succeeded","worker":1}
```

事件类型：`task.submitted`、`task.spawned`、`task.started`、`task.completed`、
`task.canceled`、`task.stolen`、`executor.shutdown`。`seq` 全局单调递增，
`GET /events` 支持 `after_seq` 增量拉取。

## 测试

```bash
go test ./...                 # 全部测试
go test -race ./...           # 含竞态检测
go test ./scheduler -v        # 逐条查看
```

### 验收项如何被覆盖

| 验收要求 | 对应测试 |
| --- | --- |
| 深递归任务树 + 单线程模式，不死锁 | `TestSingleWorkerDeepRecursionTree`（depth 9，1023 节点）、`TestSingleWorkerLinearChain`、`commands_test` depth 10（2047 节点，workers=1 与 4） |
| 随机取消 | `TestRandomCancellationSingleWorker` / `TestRandomCancellationMultiWorker`：随机构造 spawn/wait 树，并发随机取消 |
| 每个任务至多执行一次 | 上述随机测试从事件流断言每个 task 的 `started` ≤1、函数调用计数一致 |
| 每个任务恰好一个终态 | 断言每个 task 恰有一条 `completed`，且 drain 后所有任务终态 |
| 最终可退出 | `WaitIdle` 归零 + `Shutdown` 返回 + 统计计数自洽 |
| 取消 pending 任务不执行 | `TestCancelPendingChildrenNeverRuns`（attempts==0） |
| 优雅/强制关闭 | `TestShutdownDrainsQueuedTasks`、`TestShutdownForcesUncooperativeTaskByTimeout` |
| 可替换时钟 | 4 个 `TestMockClock*` + `TestSleepWithMockClock` |
| 窃取扩散 | `TestStealingSpreadsWork`（断言 4 个 worker 都参与、有 steal 事件） |
| HTTP 接口 | `server/server_test.go`（提交/查询/取消/事件/列表/错误码） |

实际运行命令与结果见 [`RUNLOG.md`](RUNLOG.md)。

## 范围与取舍

- 纯后端：无 UI、无前端资源。
- panic 被捕获并隔离，panic 的任务记为 `panicked`，不影响 worker 与后续任务。
- 强制关闭不能杀死无视 ctx 的任务（Go 无法安全强杀 goroutine）；`Shutdown`
  会取消任务 context 并等待其真正退出，这是有意为之的安全语义。
- 事件投递在执行器锁内同步完成以保证“终态先于事件可见”的强序；内置 sink
  （内存环、带缓冲通道、JSON 编码）在常见情形下非阻塞，自定义慢 sink 会对
  调度产生背压。
