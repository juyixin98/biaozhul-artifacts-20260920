# dagscheduler — 带依赖失败传播的 DAG 作业调度库

纯后端项目（Go，无前端）：一个可测试的 DAG 作业调度库 + 本地 HTTP 接口。

## 功能

- **DAG 执行**：按依赖拓扑调度节点，同层节点并发执行。
- **重试预算**：每个节点独立的 `maxAttempts`（总尝试次数，含首次），失败后按可替换的 backoff 策略等待再重试。
- **依赖策略**：
  - `ALL_SUCCESS`（默认）：所有依赖成功才运行；某依赖失败或被跳过时，本节点不执行并标记为 **SKIPPED**，跳过沿依赖链继续传播；真正执行并重试耗尽的节点才是 **FAILED**；
  - `ALL_END`：所有依赖结束（无论成功/失败/跳过）即运行。
- **失败传播区分跳过与失败**：真正执行并重试耗尽的节点为 `FAILED`；因依赖失败而根本没有运行的节点为 `SKIPPED`，二者在状态、事件与作业终态中均区分。
- **启动前检测环**：提交时做静态校验（重名、未知依赖、自依赖、重复依赖、非法策略、有向环），有环直接拒绝，不执行任何节点。
- **取消**：`Cancel()` 通过 context 通知运行中的执行器；尚未启动的节点标记 SKIPPED；backoff 等待可被立即打断（不会多发一次执行）。
- **可替换的时钟与执行器**：核心库只依赖 `Executor`、`Clock` 两个接口。测试用 `FakeClock`（手动推进时间）和可控脚本执行器，无需 sleep 即可确定性验证重试/取消时序。
- **结构化事件记录**：每次作业/节点状态变更产生一条 `Event`（序号、时间戳、类型、节点、尝试次数、状态、错误、原因、backoff 时长），通过 `Sink` 接口输出；HTTP 层用内存 Sink 提供事件查询。
- **核心不变量：节点不会重复并发运行**——同一节点在任意时刻最多只有一个 `Execute` 调用，重试也是上一次尝试完全结束、backoff 结束后才启动下一次。有专门的并发计数测试证明。

## 目录结构

```
.
├── go.mod
├── scheduler/                 # 核心库（无 HTTP、无具体执行器依赖）
│   ├── types.go               # 状态/策略/事件/接口定义
│   ├── validate.go            # 静态校验 + 环检测（迭代 DFS 三色标记）
│   ├── clock.go               # RealClock + FakeClock（可推进的虚拟时钟）
│   ├── engine.go              # Engine：提交、查询、取消
│   ├── job.go                 # 调度循环、重试、失败传播、取消处理
│   ├── api.go                 # Job 公开 API、快照、SliceSink
│   ├── engine_test.go         # 验收测试（环/菱形/取消/重试/并发）
│   └── exec_test_helpers_test.go
├── internal/
│   ├── executor/demo.go       # 演示执行器：ok/fail/flaky/sleep
│   └── server/server.go       # HTTP 接口
├── cmd/server/main.go         # 服务入口
└── examples/                  # 请求样例 JSON
```

## 构建与运行

```bash
go build ./...
go run ./cmd/server -addr 127.0.0.1:8080
```

可选参数：`-backoff`（重试间隔，默认 100ms）、`-event-cap`（内存事件上限，默认 10000）。

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 健康检查 |
| POST | `/v1/jobs` | 提交 DAG（校验失败/有环返回 400） |
| GET | `/v1/jobs` | 列出所有作业及节点状态 |
| GET | `/v1/jobs/{id}` | 查询单个作业快照 |
| POST | `/v1/jobs/{id}/cancel` | 请求取消（已结束返回 409） |
| GET | `/v1/jobs/{id}/events` | 该作业的结构化事件流 |

### 快速体验

```bash
# 提交菱形 DAG
curl -s -XPOST localhost:8080/v1/jobs \
  -H 'Content-Type: application/json' \
  -d @examples/diamond.json

# 失败传播：risky 失败 → needs-everything 被 SKIPPED；ALL_END 节点照常运行
curl -s -XPOST localhost:8080/v1/jobs \
  -H 'Content-Type: application/json' \
  -d @examples/failure-propagation.json

# 重试：前两次失败、第三次成功（maxAttempts=4）
curl -s -XPOST localhost:8080/v1/jobs \
  -H 'Content-Type: application/json' \
  -d @examples/retry.json

# 取消长作业
curl -s -XPOST localhost:8080/v1/jobs -H 'Content-Type: application/json' \
  -d @examples/cancel.json        # 返回 {"id":"job-N",...}
curl -s -XPOST localhost:8080/v1/jobs/job-N/cancel
curl -s localhost:8080/v1/jobs/job-N/events
```

### 请求体字段

```json
{
  "name": "可选的作业名",
  "nodes": [
    {
      "name": "节点名（作业内唯一）",
      "deps": ["依赖的节点名"],
      "policy": "ALL_SUCCESS | ALL_END（默认 ALL_SUCCESS）",
      "maxAttempts": 3,
      "payload": { "action": "ok|fail|flaky|sleep", "...": "由执行器解释" }
    }
  ]
}
```

演示执行器支持的 payload：

| action | 字段 | 行为 |
|---|---|---|
| `ok` | — | 立即成功 |
| `fail` | `message` | 总是失败 |
| `flaky` | `failTimes`, `message` | 前 N 次失败，之后成功（用于演示重试） |
| `sleep` | `sleepMs` | 睡眠，响应取消（用于演示取消） |

## 测试

```bash
go test -race -count=1 ./...
```

测试覆盖（`scheduler/engine_test.go`）：

1. `TestRejectsDirectedCycle` / `TestRejectsSelfCycle` / `TestRejectsUnknownDep` —— 环与静态校验，且有环时执行器零调用；
2. `TestDiamondAllSuccessRunsInOrder` —— 菱形依赖，A 先于 B/C，D 在 B、C 都结束后运行；
3. `TestAllSuccessPropagatesSkip` —— B 失败时 D 被 SKIPPED 且从不执行，作业 FAILED；
4. `TestAllEndRunsDespiteFailedDep` —— ALL_END 策略下依赖失败 D 仍运行并成功；
5. `TestAllSuccessSkipChain` —— 跳过沿依赖链传递；
6. `TestRetryThenSucceed` / `TestRetryBudgetExhausted` —— 重试预算（尝试序号 [1,2,3]、预算耗尽即 FAILED）；
7. `TestNoDuplicateConcurrentExecution` / `TestNoConcurrentAcrossRetriesWithFakeClock` —— **并发不重复不变量**：执行器记录每个节点的活跃并发数与调用次数，阻塞场景与 FakeClock 重试场景下最大并发均为 1；
8. `TestCancelStopsRunningNode` / `TestCancelBeforeNodeStartsSkipsIt` / `TestCancelDuringRetryBackoff` —— 取消运行中节点（context 被感知）、未启动节点跳过、backoff 中取消不产生额外执行；
9. `TestEventSequenceAndContent` —— 事件序号连续、带时间戳与错误内容；
10. HTTP 集成测试（`internal/server/server_test.go`）：提交/环拒绝 400/flaky 重试/失败传播/ALL_END/取消睡眠作业/取消已结束作业 409/404/列表/事件。

## 设计要点

- **调度模型**：每个作业一个调度 goroutine + 每节点尝试一个执行 goroutine。所有节点状态在 `Job.mu` 下变更；执行 goroutine 只通过带缓冲的 `reports` 通道回报，不直接改状态。
- **依赖计数**：每个 pending 节点维护未结束依赖数 `remaining`；依赖结束时递减，归零且策略允许即启动；策略不满足则标记 SKIPPED 并把跳过沿依赖图做 BFS 级联。
- **取消信号双通道**：`context` 负责打断执行器/时钟等待；调度器自身监听一个独立的 `cancelCh`（关闭即广播），避免与 Go context 内部锁在持锁路径上交互。
- **FakeClock**：`Sleep` 注册等待者，`Advance` 推进时间并唤醒到期者；context 取消由独立 watcher 仅关闭一个本地 channel 唤醒，不在回调中触碰引擎锁，杜绝锁倒置。
- **作业终态优先级**：取消请求优先 → `CANCELED`；否则存在 FAILED 节点 → `FAILED`；其余（只有 SUCCEEDED/SKIPPED）→ `SUCCEEDED`。
