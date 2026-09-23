# 运行报告（RUN_REPORT）

记录本项目的实际构建、测试与端到端运行情况。环境：

- OS：Linux 6.8.0-90-generic (amd64)
- Go：go1.22.2 linux/amd64
- 日期：2026-09-23
- 依赖：仅 Go 标准库，无第三方模块

## 1. 构建与静态检查

```
$ go vet ./...
vet OK

$ go build ./...
build OK

$ go build -o aq-server ./cmd/server
（成功生成二进制）
```

## 2. 自动化测试

```
$ go test -race -count=1 ./...
?       agingqueue/cmd/server    [no test files]
ok      agingqueue       1.101s
```

19 个测试用例全部通过（`go test -race -count=1 -v .` 实测）：

```
--- PASS: TestAcceptance_LowPriorityRunsUnderSustainedHighPriority (0.02s)
--- PASS: TestAcceptance_FIFOWithinSamePriority (0.00s)
--- PASS: TestAcceptance_RetryKeepsAgeAnchor (0.00s)
--- PASS: TestClockJump_MultipleLevelsAndBackoff (0.00s)
--- PASS: TestDuplicateCancel_Queued (0.02s)
--- PASS: TestDuplicateCancel_Running (0.00s)
--- PASS: TestCancel_Delayed (0.00s)
--- PASS: TestRetriesExhausted (0.00s)
--- PASS: TestSubmitValidation (0.00s)
--- PASS: TestPriorityCapAndTieBreak (0.02s)
--- PASS: TestCancelUnblocksRunningJob_DoesNotLeak (0.00s)
--- PASS: TestContextCanceledTreatedAsFailureUnlessCanceledJob (0.00s)
--- PASS: TestHTTP_SubmitGetListCancel (0.00s)
--- PASS: TestHTTP_Validation (0.00s)
--- PASS: TestHTTP_DuplicateIDConflict (0.00s)
--- PASS: TestHTTP_CancelQueuedAndRepeated (0.00s)
--- PASS: TestHTTP_MetricsAndHealth (0.00s)
--- PASS: TestHTTP_EventsEndpoint (0.00s)
--- PASS: TestHTTP_SleepJobAdvancesWithFakeClock (0.00s)
```

覆盖率与确定性压力：

```
$ go test -cover .
ok      agingqueue      coverage: 78.7% of statements

$ go test -race -count=30 .
ok      agingqueue      3.461s        # 30 轮竞态检测全部通过，无抖动

$ go test -count=300 .
ok      agingqueue      21.7s         # 300 轮重复全部通过
```

未覆盖部分主要是 `cmd/server/main.go` 的进程装配与信号处理（薄入口）、
`RealClock`（对标准库 time 的薄封装），以及部分防御性分支。

## 3. HTTP 服务端到端实测

启动（`aging-step=1s`，并发 2）：

```
$ ./aq-server -addr 127.0.0.1:<port> -aging-step 1s -max-concurrency 2
2026/... agingqueue listening on 127.0.0.1:<port> (aging-step=1s concurrency=2)
```

### 3.1 基本链路

- `GET /healthz` → `{"status":"ok"}`
- `POST /jobs` echo：201，随后 `GET` 显示 `state=succeeded`、结果回显。
- `POST /jobs` flaky（失败 1 次）：约 1 个退避后查询，
  `state=succeeded attempts=2 last_error="simulated failure (attempt 1)"`，
  事件链包含 `failed → retry_scheduled → ready → started → succeeded`。

### 3.2 核心验收：持续高优先级下低优先级老化运行（真实时钟）

用 2 个 `sleep(15s)` 占满并发槽，提交 `low(priority=0)`，再连续注入
`h1/h2/h3(priority=9)`。轮询 `low` 的有效优先级，实测：

```
~3s : LOW effective_priority/state = 3 queued
~6s : LOW effective_priority/state = 6 queued
~9s : LOW effective_priority/state = 9 queued
~12s: LOW effective_priority/state = 9 queued
```

约 15s 占槽作业结束后，`started` 事件顺序：

```
23:50:02.340 hold1
23:50:02.347 hold2
23:50:17.341 low      <- 老化到 9 且入队最早，先派发
23:50:17.341 h1
23:50:17.341 h2
23:50:17.341 h3
```

四个作业最终均 `succeeded`，`low` 的结果为 `low-done`。
事件日志中 `low` 有 9 条 `priority_boosted`（0→9）。

### 3.3 重复取消

占满槽位制造排队作业 `victim2`：

```
第一次取消 : outcome=canceled state=canceled
重复取消   : HTTP 409, error="agingqueue: job already canceled"
再重复一次 : HTTP 409
取消不存在 : HTTP 404
```

且对已终态作业的重复取消不新增事件（有断言覆盖）。

### 3.4 指标与事件

`GET /metrics` 实测返回各状态计数；`GET /events` 一次运行中观测到
`submitted: 10, started: 11, succeeded: 9, priority_boosted: 9,
failed: 1, retry_scheduled: 1, ready: 1` 等事件，与上述操作吻合。

## 4. 未通过项 / 已知限制

- 最终版本 **无未通过测试**：`go test -race`、30 轮 race 压力、
  300 轮重复运行均通过。
- 开发过程中曾出现基于 goroutine 调度的时序抖动；已通过两处设计改进
  从根上消除，而非加 sleep 规避：
  1. `FakeClock` 改为 `AfterFunc` 回调式，到期回调在 `Advance`
     调用栈内**同步内联**执行；
  2. 增加 `Config.SynchronousExec`，假时钟单元测试下作业同步执行。
- 已知限制（设计如此，非缺陷）：
  - 纯内存状态，无持久化，重启后作业与事件丢失。
  - `AttemptTimeout` 基于 context 的真实时钟，FakeClock 下不会触发。
  - HTTP 服务为单实例本地接口，未做鉴权/多租户/水平扩展。
