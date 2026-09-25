# 运行记录（RUNLOG）

本文件如实记录在交付环境中的实际命令与结果。环境：

- OS：Linux 6.8.0-90-generic (amd64)
- Go：`go version go1.22.2 linux/amd64`
- 日期：2026-09-23

## 1. 构建与静态检查

```text
$ go version
go version go1.22.2 linux/amd64

$ go build ./...
（无输出，成功）

$ go vet ./...
vet clean

$ gofmt -l .
（无输出，全部已格式化）
```

产物：`bin/wsd`（约 7.7 MB，纯静态风格，无第三方依赖，`go.mod` 无 require）。

## 2. 自动化测试

### 2.1 竞态检测全套

```text
$ go test -race ./... -count=1
?   	worksteal/cmd/wsd	[no test files]
ok  	worksteal/commands	1.179s
ok  	worksteal/scheduler	6.243s
ok  	worksteal/server	1.069s
```

全部通过，`-race` 无任何数据竞争报告。共 33 个测试函数：

- `scheduler`：22 个（时钟 4、deque 2、执行器基础 4、单线程深递归/链 2、
  取消 3、窃取扩散 1、关闭 2、mock 睡眠 1、随机取消 2 等）
- `server`：9 个 HTTP 端到端
- `commands`：2 个（通过注册的任务类型跑 depth=10、2047 节点单/多 worker）

### 2.2 随机取消压测重复 10 轮（多种子、随机树、随机取消）

```text
$ go test ./scheduler/ -run 'TestRandomCancellation' -count=10
ok  	worksteal/scheduler	0.294s
```

典型一轮的统计（单 worker）：

```text
workers=1 created=83 started=62 succeeded=34 failed=28 panicked=0 canceled=21 cancelCalls=76 cancelOK=73
```

多 worker 典型：

```text
workers=6 created=292 started=272 succeeded=233 failed=0 panicked=0 canceled=59 cancelCalls=34 cancelOK=31
```

（注：不同轮次因种子不同数字会变；失败=任务函数返回错误，与 panic/取消相互独立。）

### 2.3 覆盖率

```text
$ go test ./scheduler/ -cover -count=1
ok  	worksteal/scheduler	5.141s	coverage: 79.3% of statements
```

未覆盖部分主要是非常规错误分支（sink 写文件错误、非法 handle 等防御路径）。

## 3. 端到端验收脚本

```text
$ bash scripts/acceptance.sh
```

脚本自动完成：`go vet` → `go test -race ./...` → 构建 → 以 **workers=1**
启动 → 提交 depth=11 的二叉递归树（4095 个任务）→ 交错提交 16 棵深树并
**随机取消 8 棵** → 从 JSONL 事件流校验不变量 → `POST /shutdown` 验证退出。

最近一次实际输出（节选）：

```text
healthz: {"ok":true,"outstanding":0,"workers":1}

root t-1 -> succeeded
子树节点数=4095 (期望 4095)

提交 16 棵任务树，其中 8 棵请求取消
最终 stats:
{
  "workers": 1, "submitted": 17, "spawned": 66616, "started": 66590,
  "succeeded": 66527, "failed": 0, "panicked": 0, "canceled": 106,
  "running": 0, "outstanding": 0, "queued_local": 0, "queued_global": 0,
  "shutting_down": false
}

-- JSONL 事件不变量校验 --
tasks with terminal state: 66633
max starts per task:  1
state distribution:    {'succeeded': 66527, 'canceled': 106}
ALL EVENT INVARIANTS HOLD: at-most-once execution, exactly-one terminal state

-- 优雅关闭 (POST /shutdown) --
{"ok":true,"status":"draining"}
服务已正常退出。

✅ 验收全部通过：单线程深递归无死锁、随机取消、至多执行一次、最终可退出。
```

> 说明：66633 个任务 = depth=11 探针 4095 + 16 棵随机深度（10–13）树。
> `max starts per task: 1` 即“每个任务至多执行一次”的直接证据；每个任务
> 恰有一条 `task.completed`（脚本中以 Python 断言校验，重复终态会立即失败）。

## 4. 手动 HTTP 走查（实际执行）

在 2-worker 与 1-worker 两个实例上分别用 `curl` 验证过：

| 场景 | 命令/结果 |
| --- | --- |
| 健康检查 | `GET /healthz` → `{"ok":true,"outstanding":0,"workers":2}` |
| 深递归 | `POST /tasks recurse depth=6` → 轮询得 `succeeded`, `value=127` |
| 失败任务 | `fail` → `state=failed, err="demo failure"` |
| panic 任务 | `panic` → `state=panicked`，后续任务仍正常（隔离成功） |
| 未知类型 | 404；非法 JSON → 400；关闭后提交 → 503 |
| 取消子树 | depth=14 任务运行中取消 → 根 `canceled (context canceled)`，全部 pending 后代直接 canceled |
| 事件 | `GET /events` 返回严格递增 seq；`-event-log` 落盘 JSONL 类型分布如 `{submitted:2, started:32, spawned:30, completed:32, stolen:4, shutdown:1}` |
| 单线程链 | `chain count=500` → `succeeded value=500` |
| 单线程深树 | depth=12（8191 任务）→ `succeeded value=8191, started=8191, outstanding=0` |
| 睡眠 | `sleep millis=200` → succeeded（墙钟约 200ms 完成） |
| 退出 | `SIGTERM` 与 `POST /shutdown` 均打印 draining 后进程正常退出，端口释放 |

## 5. 开发过程中发现并修复的问题（如实记录）

以下问题均在开发过程中由测试暴露并修复，**最终代码与全套测试均通过**，
列此以保持记录诚实：

1. **事件 seq 乱序**：初版事件在执行器锁外通过每-sink goroutine 投递，
   多 worker 下小 seq 事件可能被大 seq 反超。改为在执行器锁内同步投递，
   seq 在锁内分配，顺序与全局序一致；测试 `TestRandomCancellationMultiWorker`
   现严格断言 seq 单调。
2. **等待/Sleep 的唤醒丢失**：等待原语初版用容量 1 的信号通道 + 非阻塞发送，
   worker 正忙（帮忙执行任务）时信号会丢；多个中间版本（缓冲通道、双 watcher）
   仍有边缘竞态与 watcher 自死锁。最终重写为教科书式 **条件变量 + 锁内谓词
   检查**（谓词由执行器锁保护，定时器/ctx 桥接持锁置位并广播），无丢失窗口。
3. **优雅关闭让运行中任务过早收到取消**：初版 Shutdown 一开始就取消所有任务
   ctx。改为 pending 立即取消、running 任务保留 ctx 自然跑完，仅在 drain
   超时后经根 ctx 协作式取消；对应测试
   `TestShutdownDrainsQueuedTasks` / `TestShutdownForcesUncooperativeTaskByTimeout`。
4. **worker 早退竞态**：worker 曾以 `shuttingDown && running==0` 为退出条件，
   在强制取消路径上会先于 Shutdown 决策退出。新增独立 `workersStop` 标志，
   只有 Shutdown 排空判定完成后才放行 worker 退出。
5. **取消语义分类**：任务返回自己的 `ctx.Err()` 初被判为 failed，现按
   `errors.Is(context.Canceled/DeadlineExceeded)` 归类为 canceled。
6. **JSONL 事件文件最初为空**：`cmd/wsd` 创建了 JSONSink 但未传入 server
   构造；已通过 `server.Config.ExtraSinks` 修复并以端到端方式验证落盘。
7. **MockClock 睡眠测试自身的竞态**：测试曾在任务尚未开始（定时器尚未创建）
   时就推进时钟，导致 deadline 被算到推进之后。这是**测试问题不是库问题**，
   改为先观察到 `MockClock.Pending()>=1`（任务已进入 Sleep）再推进时钟。

## 6. 未通过项 / 已知限制

- 最终状态：**无未通过测试**。`go test -race ./...` 全绿，验收脚本通过。
- 已知设计限制（非缺陷）：
  - 无视 `ctx.Done()` 的任务无法被强杀（Go 无法安全终止 goroutine）；
    `Shutdown` 在超时后只能取消其 context 并等待其自行退出。
  - 内存事件为定长环形缓冲（默认 4096），超量旧事件被覆盖；需要完整流水时
    使用 `-event-log` JSONL 落盘。
  - 仅监听本机 loopback，无 TLS/鉴权——定位为本地后端调度库与接口。
