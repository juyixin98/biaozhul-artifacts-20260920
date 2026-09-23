# agingqueue —— 带老化（aging）的离散优先级作业队列

纯后端、无前端、无外部依赖（仅标准库）的 Go 调度库与本地 HTTP 服务。

核心能力：

- **离散优先级**：优先级 `0..9`，数字越大越紧急。
- **等待老化**：作业在就绪队列中每等待一个 `aging-step`，**有效优先级 +1**，
  封顶 9。低优先级任务不会被持续到来的高优先级任务永久饿死。
- **同优先级 FIFO**：有效优先级相同时，严格按**首次入队的单调序号**派发。
- **重试不重置年龄**：重试沿用同一作业 ID、同一入队序号、同一累计等待；
  退避期间不计入等待年龄。因此重试无法靠"重新入队"把队尾变队头、
  长期插队。
- **时钟与执行器可替换**：`Clock`（真实时钟 / 可任意跃迁的假时钟）、
  `Executor`（按作业类型注册）都是接口。
- **结构化事件记录**：每次状态变更都产生带全局序号、时间戳的事件，
  支持历史查询与 SSE 实时订阅。

---

## 目录结构

```
.
├── go.mod                 # module agingqueue（无第三方依赖）
├── clock.go               # Clock 接口、RealClock、FakeClock（假时钟）
├── types.go               # 作业/状态/优先级/执行器接口
├── heaps.go               # 就绪堆、退避堆、老化跳档堆
├── scheduler.go           # 调度器核心（派发、老化、重试、取消、关闭）
├── events.go              # 结构化事件日志与订阅
├── executors.go           # 内置执行器 echo / sleep / flaky
├── server.go              # 本地 HTTP 接口
├── view.go                # 对外只读快照
├── cmd/server/main.go     # HTTP 服务入口
├── examples/
│   ├── requests.md        # 全部接口的 curl 请求样例
│   └── smoke.sh           # 可运行的端到端冒烟脚本
├── acceptance_test.go     # 验收测试（老化/FIFO/重试锚点）
├── scheduler_test.go      # 时钟跃迁、重复取消、重试耗尽等
├── server_test.go         # HTTP 接口测试
└── testhelpers_test.go    # 测试用执行器与同步辅助
```

---

## 快速开始

需要 Go 1.22+。

```bash
# 运行全部测试（含竞态检测）
go test -race ./...

# 启动 HTTP 服务
go run ./cmd/server -addr :8080 -aging-step 1s -max-concurrency 4

# 或先构建
go build -o aq-server ./cmd/server
./aq-server -addr :8080
```

启动参数（也可用环境变量 `AGING_STEP` / `MAX_CONCURRENCY` /
`MAX_ATTEMPTS` / `BACKOFF` / `MAX_BACKOFF`）：

| 参数 | 默认 | 说明 |
|------|------|------|
| `-addr` | `:8080` | 监听地址 |
| `-aging-step` | `1s` | 就绪等待每满此时长，有效优先级 +1 |
| `-max-concurrency` | `4` | 同时运行的作业上限 |
| `-max-attempts` | `3` | 默认最大尝试次数 |
| `-backoff` | `200ms` | 初始重试退避，之后指数翻倍 |
| `-max-backoff` | `5s` | 重试退避上限 |

---

## HTTP 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET  | `/healthz` | 健康检查 |
| POST | `/jobs` | 提交作业（201）；未知类型/坏参数 400，重复 ID 409 |
| GET  | `/jobs` | 列出全部作业（按入队序号） |
| GET  | `/jobs/{id}` | 查询单个作业（404） |
| POST | `/jobs/{id}/cancel` | 取消（200）；终态重复取消 409；不存在 404 |
| GET  | `/events` | 事件日志，支持 `limit`、`before_seq`、`stream=1`（SSE） |
| GET  | `/metrics` | 各状态计数 |

完整 curl 样例见 [`examples/requests.md`](examples/requests.md)，
可运行演示见 [`examples/smoke.sh`](examples/smoke.sh)。

取消的返回语义（`outcome` 字段）：

- `canceled`：作业在排队/退避，已立即进入终态；
- `cancel_requested`：作业运行中，已调用其 context 取消，终态稍后落定；
- `already_canceling`：运行中的作业此前已收到取消（重复取消、幂等）；
- 对已终态作业再次取消返回 **HTTP 409**，且**不产生新事件**。

---

## 调度语义

### 有效优先级

```
effective = min(9, base + floor(累计就绪等待 / aging_step))
```

- 只有处于**就绪队列**中的等待计入年龄；
- **运行中**与**重试退避中**的时间不计入；
- 每到一个整档点触发一次 `priority_boosted` 事件并在堆中重排。

### 派发顺序

就绪堆的比较键为 `(effective_priority 降序, seq 升序)`：

1. 有效优先级高者先跑；
2. 有效优先级相同，首次入队序号小者（FIFO）先跑。

### 为什么重试不能靠重置年龄插队

作业的年龄锚点 `enqueuedAt` 与 FIFO 序号 `seq` 在**首次入队时确定，
生命周期内永不改变**。失败后重试：

- 不换 ID、不换 `seq`；
- 历史就绪等待 `accWait` 保留，退避结束后从既有年龄继续老化；
- 退避的那段时间（不在就绪队列）不计入等待。

因此一个反复失败的作业，最多只能凭"它真的在就绪队列里等了那么久"
而老化提权，无法通过"失败→重新排队"刷新序号把后来的同级作业挤到后面。

### 时钟跃迁的确定性

`FakeClock` 的时间只在 `Advance(d)` 时前进，且到期回调在 `Advance`
的**同一调用栈内同步执行**。一次大跃迁会按时间顺序补齐其间所有事件：
退避到期 → 回到就绪 → 老化逐档提升 → 满足条件即派发。这让"时钟大跳"
在测试里是确定且可断言的（见 `TestClockJump_MultipleLevelsAndBackoff`）。

库还提供 `Config.SynchronousExec`：开启后作业在派发调用栈内同步执行，
配合假时钟使"派发→完成→重试"整条链路在一次提交/跃迁内确定完成
（单元测试使用；生产用默认的异步 goroutine 执行）。

---

## 作为库使用

```go
package main

import (
    "context"
    "time"

    "agingqueue"
)

type myExecutor struct{}

func (myExecutor) Execute(ctx context.Context, j *agingqueue.JobHandle) error {
    // j.Priority 是基础优先级；j.Attempt 从 1 开始；j.EnqueuedAt 永不改变
    _ = j.Payload
    return nil
}

func main() {
    s := agingqueue.NewScheduler(agingqueue.Config{
        Clock:          agingqueue.NewRealClock(), // 或 NewFakeClock(...)
        AgingStep:      time.Second,
        MaxConcurrency: 4,
    })
    s.RegisterExecutor("my-type", myExecutor{})
    s.Start()
    defer s.Close()

    s.Submit(agingqueue.SubmitOptions{
        Type: "my-type", Priority: 2, Payload: []byte(`{}`),
    })
}
```

自定义执行器只需实现：

```go
type Executor interface {
    Execute(ctx context.Context, job *JobHandle) error
}
```

需要回传结果数据时，可额外实现
`ExecuteResult(ctx, *JobHandle) ([]byte, error)`。

---

## 测试

```bash
go test -race -count=1 ./...        # 常规 + 竞态
go test -count=100 .                # 重复运行验证确定性
go test -v -run TestAcceptance .    # 只看验收用例
go test -cover ./...                # 覆盖率
```

主要测试与需求的对应关系：

| 测试 | 覆盖的需求 |
|------|-----------|
| `TestAcceptance_LowPriorityRunsUnderSustainedHighPriority` | 持续注入高优先级，低优先级在老化条件满足后可运行；老化前不运行；含 9 次跳档事件 |
| `TestAcceptance_FIFOWithinSamePriority` | 相同（有效）优先级严格 FIFO，后到者不插队 |
| `TestAcceptance_RetryKeepsAgeAnchor` | 重试不换 ID/序号、保留累计等待；老化后凭 FIFO 先于同级新作业 |
| `TestClockJump_MultipleLevelsAndBackoff` | 单次时钟大跃迁跨越多档老化 + 退避窗口，状态全部补齐、顺序正确 |
| `TestDuplicateCancel_Queued` / `_Running` / `TestCancel_Delayed` | 排队/运行/退避三种取消，重复取消幂等且无新事件 |
| `TestRetriesExhausted` | 指数退避（100ms→200ms）、耗尽后 failed、完整事件链 |
| `TestContextCanceledTreatedAsFailureUnlessCanceledJob` | 执行器返回 context.Canceled 但非用户取消时按可重试失败处理 |
| `TestCancelUnblocksRunningJob_DoesNotLeak` | 取消真正取消 context，Close 不挂死、无 goroutine 泄漏 |
| `server_test.go` | HTTP 提交/查询/列表/取消/校验/指标/事件/SSE 路由 |

实际运行命令与结果见 [`RUN_REPORT.md`](RUN_REPORT.md)。
