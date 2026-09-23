# Aging Priority Queue（带老化优先队列）

纯后端作业调度库 + 本地 HTTP 接口，Go 实现。核心是一个**离散优先级、等待老化**
的作业队列：低优先级作业等待越久，有效优先级越高，从而在持续注入高优先级
作业的情况下也能在规定条件下被调度，避免饥饿。调度时钟与执行器均可替换，
所有状态变更都产出结构化事件记录。

## 调度模型

- **离散优先级**：整数区间 `[MinPriority, MaxPriority]`（默认 0..9），数值大者优先。
- **有效优先级（老化）**：
  `effective = BasePriority + floor(累计等待时间 / AgeInterval)`，上限为该作业的
  `MaxPriority`（默认等于全局上限）。默认 `AgeInterval = 1s`，即每等待一个
  区间有效优先级 +1。
- **等待时间累计规则**：作业处于队列中（含初始延迟与重试退避）时累计等待；
  仅在某次尝试**执行期间**冻结。
- **同优先级 FIFO**：有效优先级相同的作业，按**不可变的提交序号**先入先出。
- **重试不重置年龄**：重试保留原始 `EnqueuedAt`、提交序号与累计等待时间。
  失败重试**不能**通过“重置年龄”把自己伪装成新作业长期插队；它也不会丢失
  已积累的等待。退避期间老化继续累计。
- **重试与死信**：失败按 `Backoff` 退避后重试，超过 `MaxAttempts` 进入
  `failed`；包装成 `queue.Fatal` 的错误（如未知作业类型）不重试，立即失败。
- **取消**：可取消排队中或运行中的作业；对已终态/不存在的 id 重复取消返回
  `409/404` 并记录 `cancel_rejected` 事件，不会产生重复的终态事件。

## 目录结构

| 路径 | 说明 |
|---|---|
| `clock/` | 可替换时钟：`Wall`（系统时钟）与 `Fake`（可手动跃迁的确定性时钟） |
| `executor/` | 可替换执行器：类型注册表 `Registry` 与内置 demo 处理器 |
| `queue/` | 核心调度器、老化/FIFO/重试逻辑、结构化事件与 Sink |
| `httpapi/` | 本地 HTTP JSON 接口（含 SSE 事件流） |
| `cmd/agingd/` | 可启动服务 |
| `*_test.go` | 单元测试与验收场景测试 |

关键扩展点（库用法）：

- `queue.Config.Clock`：注入 `clock.Fake` 做确定性测试，生产用 `clock.Wall`。
- `queue.Config.Executor`：实现 `queue.Executor`（`Execute(ctx, *Job) error`），
  或用 `executor.Registry` 按 `Job.Type` 注册处理器。
- `queue.Config.Sink`：接收结构化事件；内置内存环形 Sink、多路 `MultiSink`、
  JSONL 文件 Sink。
- `queue.Config.Backoff`：`ConstantBackoff` / `ExponentialBackoff` 或自定义。

## 构建与运行

```bash
go build ./...
go test ./...                 # 单元 + 验收测试
go test ./... -race -count=10 # 竞态检测 + 重复运行（开发时使用）

go run ./cmd/agingd -addr :8080 -age-interval 1s -concurrency 1
# 可选：-events-file events.jsonl 把结构化事件追加落盘
# 所有 flag 均可用 AGINGD_* 环境变量覆盖（见 cmd/agingd/main.go 顶部注释）
```

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| POST | `/jobs` | 提交作业 |
| GET | `/jobs` | 列出全部作业（含终态） |
| GET | `/jobs/{id}` | 查询单个作业 |
| POST | `/jobs/{id}/cancel` | 取消作业 |
| GET | `/stats` | 队列深度（按有效优先级分桶） |
| GET | `/events` | 结构化事件（内存 Sink 快照） |
| GET | `/events?stream=1` | SSE 实时事件流 |

提交体字段（时长接受 `"500ms"` 字符串或纳秒整数）：

```json
{
  "type": "echo",
  "payload": {"delay": "20ms"},
  "priority": 3,
  "maxPriority": 9,
  "maxAttempts": 3,
  "delay": "0s",
  "id": "可选-自定义ID"
}
```

内置 demo 作业类型：`noop`（立即成功）、`echo`（可选 `payload.delay`）、
`fail`（永远失败，用于观察重试/死信）、`flaky`（前 `payload.failTimes`
次失败后成功）。

## 结构化事件

每次状态变更产出一条 `Event`：`submitted / promoted / started / retrying /
succeeded / failed / canceled / cancel_rejected`。事件为有序 JSON：

```json
{"time":"...","type":"promoted","jobId":"low","detail":{"from":3,"to":4}}
```

## 验收场景如何被验证

对应自动化测试在 `queue/scheduler_test.go`：

1. **持续注入高优先级，低优先级在规定条件下可运行**
   `TestAgingPromotesLowPriority`：锚点作业占住唯一 worker，期间持续注入
   优先级 9 的作业；一个优先级 0 的作业经时钟跃迁老化到 9 后，因 FIFO
   （提交更早）在锚点释放后**先于**所有高优先级作业运行。
2. **未满足条件时保持饥饿**
   `TestLowPriorityStarvesUntilAgingCondition`：低优先级作业 `maxPriority`
   被封顶在高优先级层之下，即使长时间老化也无法越过仍在竞争的高优先级作业；
   等高优先级清空后才运行。
3. **时钟跃迁**
   所有老化测试用 `clock.Fake` 一次跨越多级（如 9s 一次性提升 9 级），
   验证级联提升与“未到期不提升”；`TestDelayedJob` 覆盖延迟到点释放。
4. **重复取消**
   `TestDuplicateCancelAndUnknownCancel`：对排队中作业取消 1 次成功，第 2、3
   次返回 `ErrTerminal`，对未知 id 返回 `ErrNotFound`；`canceled` 事件恰好 1
   条、`cancel_rejected` 事件计数正确。另有运行中取消与排队中取消
   （取消后绝不执行）用例。
5. **重试不重置年龄**
   `TestRetryDoesNotResetAge`：作业等待老化到有效优先级 2 后失败重试，
   第一次 `retrying` 事件携带保留的 `age=2s`、`effectivePriority=2`，
   `EnqueuedAt` 跨重试不变，且不会被更晚提交的作业反超。
6. **FIFO 与优先级**
   `TestHigherPriorityDispatchesFirst`、`TestFIFOWithinSamePriority`。

另外通过实际启动 `agingd` 的端到端手工验证了上述老化插队、单次派发
（修复过一个“老化迁移桶时残留旧桶条目导致重复执行”的缺陷，已有回归断言）、
重试退避、重复取消和致命错误路径。
