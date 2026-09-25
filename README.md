# rsrv — 资源预约冲突求解（纯后端）

时间区间上的多维容量资源预约库 + 本地 HTTP 接口。任务需要**连续时段**与**多维容量**；
时间一律采用**半开区间 `[start, end)`**：在 `t` 结束的预约与在 `t` 开始的预约**不冲突**。

纯 Go 标准库实现，无第三方依赖；不包含任何前端。

- 最早可行位置查询（`earliest`）与固定时段预约（`fixed`）
- **原子批量预约**：批次内任一单项无法放置，则整批拒绝、零落地
- 调度时钟（`Clock`）与生命周期执行器（`Executor`）均为接口，可替换（测试用假时钟/记录执行器）
- 每次状态变更输出**结构化事件**（内存事件日志，可同时追加 JSONL 文件）
- 调度算法与**离散小时间轴穷举参考实现**做随机差分对拍

## 目录结构

```
sched/      调度核心库（区间/容量算法、原子批次、时钟、执行器、事件）
brute/      离散小时间轴穷举参考求解器（验收对拍用，刻意写得简单）
server/     本地 HTTP 接口（net/http，Go 1.22 方法路由）
cmd/rsvrvd/ 服务入口
examples/   请求样例 JSON 与一键演示脚本 demo.sh
```

## 快速开始

需要 Go 1.22+。

```bash
go test ./...                 # 运行全部自动化测试
go run ./cmd/rsvrvd -addr 127.0.0.1:8080
# 可选：-events ./events.jsonl 把结构化事件同时追加为 JSONL
```

另开终端：

```bash
./examples/demo.sh            # 用样例依次调用各接口
```

## 语义约定

- **半开区间**：`[a,b)` 与 `[b,c)` 相邻，不重叠；重叠判定 `a.Start < b.End && b.Start < a.End`。
- **多维容量**：资源容量与任务需求都是 `map[string]int64`。在任一被需求的维度上
  `已有负载 + 新需求 > 容量` 即不可行；容量允许为 **0**，此时该维度任何正需求都不可行
  （即使时间轴为空，也会返回 `reservation_id` 为空的合成冲突，标明失败维度）。
- **连续时段**：任务占用一整段 `[start, start+duration)`，求解器不拆分任务。
- **需求**允许某维度为 0（表示不占用），不允许负数；容量不允许负数。
- **容量占用与生命周期状态解耦**：已提交预约（含 `completed`、`activation_failed`）
  始终占用其历史区间容量。生命周期状态只描述执行器侧状态。因此允许对过去窗口查询，
  结果仍与穷举一致。

## 算法

固定时段放置：把资源时间轴在所有预约起止点切片，逐片求聚合负载，任一片
`负载+需求 > 容量` 即冲突，并给出造成该片超载的具体预约、重叠区间、负载/容量/失败维度。

最早可行位置（`sched/earliest.go`）：在每个常量负载片 `[u,v)` 上，超载片恰好禁止起点

```
s ∈ (u − D, v)     （两端开区间：在 v 或 u−D 处“相邻”是允许的）
```

对这些开区间做并集（仅在严格重叠时合并；仅端点相触不合并，因为触点可行），
区间外的最早点即答案；闭窗口再与 `[windowStart, windowEnd−D]` 比较。
复杂度约 `O((n+k) log n)`。

对拍验收（`sched/differential_test.go`、`sched/batch_differential_test.go`）：随机生成
1–3 维容量（刻意高频包含容量 0）、随机已有预约与查询，扫描线结果必须与 `brute`
逐槽穷举在**可行与否**和**最早起点**上完全一致；批次级对拍还校验每个计划落点与
“拒绝时落地数为零”。

## 可替换部件

```go
type Clock interface { Now() time.Time }          // SystemClock / FakeClock
type Executor interface {                          // NopExecutor 为默认
    Activate(r *Reservation) error                 // 到 start：失败 -> activation_failed（仍占容量）
    Complete(r *Reservation) error                 // 到 end：-> completed
}
type Sink interface { Write(ev Event) }            // EventLog / JSONLSink / MultiSink
```

`Scheduler.Pump()` 按当前时钟执行到期迁移（`scheduled → active → completed`）。
提交批次后会自动 Pump 一次；驱动假时钟的测试在推进时间后显式 Pump。
执行器在存储锁之外被调用，可安全回调调度器。

事件类型：`resource_added`、`batch_committed`、`reserved`、`batch_rejected`、
`activated`、`activation_error`、`completed`，均带单调递增 `seq` 与时间戳。

## HTTP 接口

所有时间为 RFC3339；时长为 Go duration 字符串（`"90m"`、`"2h30m"`）。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/v1/resources` | 新建资源 |
| GET  | `/v1/resources` / `/v1/resources/{id}` | 列表 / 单个 |
| POST | `/v1/earliest` | 最早可行位置查询（只读，不落地） |
| POST | `/v1/reservations:batch` | 原子批量预约 |
| GET  | `/v1/batches/{id}` | 查询批次及其计划 |
| GET  | `/v1/reservations` / `/v1/reservations/{id}` | 列表（`?resource_id=`）/ 单个 |
| POST | `/v1/pump` | 立即执行到期生命周期迁移 |
| GET  | `/v1/events` | 结构化事件（`?after_seq=N` 增量） |
| GET  | `/v1/snapshot` | 全库一致快照 |
| GET  | `/healthz` | 存活检查 |

批次单项两种 `kind`：

- `fixed`：给 `interval`，冲突则整批 409。
- `earliest`：给 `duration`、`window_start`、可选 `window_end`（省略表示开放窗口），
  返回求解出的 `start/end`。

冲突时 HTTP 409，响应体的 `batch.items[]` 逐项列出原因（`capacity_exceeded` /
`no_feasible_slot`）及容量明细；此时**没有任何单项落地**。

### 最小示例

```bash
curl -s -XPOST localhost:8080/v1/resources -H 'Content-Type: application/json' \
  -d '{"id":"room-a","capacity":{"seats":10,"mics":2}}'

curl -s -XPOST localhost:8080/v1/earliest -H 'Content-Type: application/json' -d '{
  "resource_id":"room-a",
  "window_start":"2026-10-01T08:00:00Z",
  "window_end":"2026-10-01T18:00:00Z",
  "duration":"90m",
  "demand":{"seats":6,"mics":1}}'

curl -s -XPOST localhost:8080/v1/reservations:batch -H 'Content-Type: application/json' -d '{
  "id":"batch-morning",
  "items":[{"id":"standup","resource_id":"room-a","kind":"fixed",
    "demand":{"seats":8,"mics":2},
    "interval":{"start":"2026-10-01T09:00:00Z","end":"2026-10-01T09:30:00Z"}}]}'
```

更多请求样例见 [`examples/`](examples/)。

## 作为库使用

```go
sch := sched.New(sched.WithClock(sched.NewFakeClock(t0))) // 默认 SystemClock + NopExecutor
sch.AddResource(&sched.Resource{ID: "R", Capacity: sched.Dims{"cpu": 2}})

b, err := sch.CommitBatch(&sched.BatchRequest{ID: "b1", Items: []sched.BatchItem{{
    ID: "job", ResourceID: "R", Kind: sched.ItemEarliest,
    Demand: sched.Dims{"cpu": 1}, Duration: 2*time.Hour,
    WindowStart: t1, WindowEnd: t2,
}}})
// err 为 *sched.BatchConflictError 时整批未提交
```

## 测试与运行结果

测试与运行命令、实际结果、未通过项的如实记录见 [RUNLOG.md](RUNLOG.md)。
