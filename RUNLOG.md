# 实际运行记录

环境：Linux 6.8.0-90-generic x86_64，Go 1.22.2，日期 2026-09-23。
以下命令均在仓库根目录实际执行，输出如实摘录；未通过项与修复过程见第 6 节。

## 1. 编译与静态检查

```
$ go build ./...
$ go vet ./...
$ gofmt -l .
（无输出）
```

结果：全部通过，无第三方依赖。

## 2. 自动化测试（含竞态检测）

```
$ go test -race -count=1 ./...
?  	dynpool/cmd/dynpoold	[no test files]
?  	dynpool/cmd/leakcheck	[no test files]
ok  	dynpool	1.158s
ok  	dynpool/internal/server	1.112s
```

26 个测试全部 PASS：

核心库（20 个）：

| 测试 | 内容 |
|---|---|
| TestBasicExecutesTasks | 基本执行、completed 计数与事件 |
| TestQueueBoundReject | 有界队列 abort；被拒任务 handle 立即结束且不运行 |
| TestShrinkWaitsForRunningTask | 3 个阻塞任务运行中 3→1：retiring 立即=2、active 保持 3，释放后稳定为 1，无泄漏 |
| TestShrinkDoesNotLoseQueuedTasks | 4 worker 全阻塞 + 20 排队任务，缩到 0 再优雅关闭：20 任务全部执行且无重复 |
| TestInterleavedSubmitResizeShutdown | 8 提交者×100 任务与 6 次 resize、shutdown 交错：accepted==executed==800、每任务恰好一次、无泄漏 |
| TestGracefulShutdownDrainsQueued | 关闭期间拒绝提交/resize、10 个排队任务全部排空、幂等 |
| TestGracefulShutdownZeroWorkers | 缩到 0 后排队任务由 drainer 兜底执行 |
| TestShutdownNowCancelsAndDrops | 2 运行任务收 context.Canceled；6 排队任务返回且不执行；幂等；停止后提交/resize 报错 |
| TestShutdownNowDropRace | 100 轮：4 阻塞 + 50 排队，取消瞬间 worker 与排空方争抢的守恒断言（dropped+ran==50，cancelled==4） |
| TestGracefulThenForceEscalation | 优雅关闭进行中升级为强制：优雅等待者收到 ErrPoolShuttingDown，终止事件为 pool.force_stopped |
| TestSubmitShutdownRaceNoStrandedTask | 50 轮：0 worker 时 Submit 与 Shutdown 交错，accepted==executed，无滞留任务 |
| TestGracePeriodTimeout | 优雅关闭超时只让调用者返回，阻塞释放后最终 stopped |
| TestRejectDiscard / TestRejectDiscardOldest / TestRejectCallerRun | 其余三种拒绝策略 |
| TestDuplicateAndInvalidIDs | 重复 id、nil 任务、非法配置 |
| TestEventOrderingAndContent | seq 单调、时间戳、任务 id、事件类型齐全 |
| TestMultiAndJSONSink / TestReplaceableClock / TestStatusSnapshot | sink 扇出、可替换时钟、状态快照 |

HTTP 层（6 个）：`TestHTTPLifecycle`（建池→阻塞→缩容→释放→优雅关闭全链路）、
`TestHTTPGracefulShutdownDrainsQueued`、`TestHTTPForceShutdown`、
`TestHTTPRejectAbort`（429）、`TestHTTPEvents`、`TestHTTPUnknownPoolAndTask`。

稳定性复跑：

```
$ go test -race -count=10 ./...
ok  	dynpool	5.347s
ok  	dynpool/internal/server	1.929s
```

## 3. goroutine 泄漏检查

```
$ go run -race ./cmd/leakcheck
baseline goroutines: 1
after 20 create/destroy cycles: 1 goroutines
accepted=796 completed=796
OK: no goroutine leak
```

20 轮"建池(4 worker)→扩容到 8→交错提交→缩到 2→扩到 6→优雅或强制关闭"后
goroutine 数与基线一致（1）；已接收任务全部完成（强制关闭轮被 drop 的任务不计入
accepted，语义正确）。

## 4. 真实 HTTP 服务端到端演示

启动（18080 端口被本机无关进程 vccsim 占用，改用随机端口；最终复验端口为 32304）：

```
$ go build -o /tmp/dynpoold ./cmd/dynpoold
$ /tmp/dynpoold -addr 127.0.0.1:32304
2026/09/23 dynpoold listening on http://127.0.0.1:32304
$ BASE=http://127.0.0.1:32304 bash examples/demo.sh
（exit=0）
```

关键状态快照（摘自实际响应）：

| 阶段 | state | target | active | retiring | queued | running | completed |
|---|---|---|---|---|---|---|---|
| 3 个阻塞任务占满 worker | running | 3 | 3 | 0 | 0 | 3 | 0 |
| 提交 5 个 sleep 后 | running | 3 | 3 | 0 | 5 | 3 | 0 |
| 缩容 3→1（阻塞未释放） | running | 1 | 3 | 2 | 5 | 3 | 0 |
| 释放阻塞、队列排空后 | running | 1 | 1 | 0 | 0 | 0 | 8 |
| 优雅关闭后 | stopped | 0 | 0 | 0 | 0 | 0 | 8 |

结论：缩容瞬间 retiring=2 而 active 仍为 3（运行中任务未被打断）；释放后
8 个已接收任务（3 阻塞 + 5 sleep）全部 completed，恰好留下 1 个 worker；
优雅关闭后无残留。

强制关闭实际响应（2 个阻塞任务运行中、4 个 sleep 排队中）：

```json
{"dropped":["task-3","task-4","task-5","task-6"],
 "status":{"state":"stopped","active_workers":0,"running_tasks":0,
           "completed_tasks":2,"cancelled_tasks":2,"dropped_tasks":4}}
```

被强制取消的任务查询结果：`{"id":"f1","state":"completed","ran":true,"error":"context canceled"}`

拒绝与关闭后语义（实际 HTTP 状态码）：

```
block submit: 202
slot submit:  202          # 占用唯一队列槽
full submit:  HTTP 429     # abort：{"state":"rejected","rejected":true,...}
graceful delete: 200
submit after stop: 409
```

## 5. 事件示例（实际结构）

事件带单调 `seq`、时间戳、池名、worker/task 关联字段。优雅关闭收尾序列：
`pool.shutdown → worker.retiring → task.completed(×N) → worker.exited(×N) → pool.stopped`；
强制收尾为 `pool.force_stopped`，运行任务另有 `task.cancelled`、排队任务有
`task.dropped`。可通过 `GET /v1/pools/{name}/events?type=...&from=...` 查询。

## 6. 开发中出现过、已修复的问题（如实记录）

1. retiring 初版在 worker **观察到**退休信号时才计数，阻塞任务运行中该状态不可见；
   已改为发信号时立即在 `workerState.retiring` 记账。
2. drainer 初版排空后仍阻塞，在"缩到 0 再优雅关闭"路径造成 Shutdown 挂起；已改为
   排空即退出。
3. caller_run 分支漏调 `Handle.markStarted()`，`Result()` 误报 ran=false；已修复并测试。
4. **Submit/Shutdown 滞留任务竞态**（审查发现）：入队原本在锁外，可能与 Shutdown 的
   "是否需要 drainer"判断交错，导致任务入队后无 worker 也无 drainer。已把入队移入
   p.mu；`TestSubmitShutdownRaceNoStrandedTask`（50 轮）在旧实现上可复现挂起，修复后通过。
5. **ShutdownNow 的 drop 竞态**（`-count=10` 压测暴露）：取消 ctx 后 worker 的 select
   可能随机选中队列接收分支，把排队任务取走执行，使 dropped 少一个。已改为 worker
   取到任务后若 ctx 已取消则走统一 `dropTask`（channel 接收排他，保证恰好记账一次），
   ShutdownNow 等 worker 退出后再排空残余；`TestShutdownNowDropRace` 100 轮守恒断言。
6. **优雅/强制互锁死锁**（新测试暴露）：Shutdown 等待排空期间仍持有 forceMu，导致随后
   ShutdownNow 永远拿不到锁去取消阻塞任务。已把等待移到锁外，并按最终状态发送
   `pool.stopped`/`pool.force_stopped`；`TestGracefulThenForceEscalation` 覆盖。
7. 两处测试自身时序问题（未等任务真正占槽/入队就断言；3 worker 中 2 阻塞 1 空闲时空闲
   worker 立即退休是正确行为）——均通过等待 `Status()` 条件、占满全部 worker 修正，
   `-count=10` 复跑无 flake。

未通过项：最终版本 `go build`、`go vet`、`gofmt`、`go test -race -count=10 ./...`、
`go run -race ./cmd/leakcheck`、真实 HTTP 演示（优雅 + 强制 + 拒绝）**均无未通过项**。
