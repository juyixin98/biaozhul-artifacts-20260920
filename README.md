# deadline-admission — 截止时间准入调度（纯后端）

单机、非抢占作业的 **EDF（最早截止时间优先）队列 + 保守准入控制** 后端库与本地 HTTP 接口，Go 实现，无前端。

- 调度时钟（`Clock`）与执行器（`Executor`）均为可替换接口：生产用真实时钟，测试用手动时钟确定性推进
- 所有状态变更写入结构化事件日志（`EventLog`），可通过 HTTP 查询
- 任务提交时声明**执行上界**（`exec_bound_ms`）与**绝对截止时间**
- 明确区分两类失败：
  - `REJECTED_INFEASIBLE` —— **预计不可满足**：准入时即判定无法保证截止时间，拒绝入网，不占用任何资源
  - `TIMEOUT` —— **运行超时**：已准入并启动，但实际执行超过声明上界，被强制停止
- 取消不重复释放资源：排队中的取消不触碰执行槽；运行中的取消只发信号，槽位由 worker 在唯一出口释放一次

## 目录结构

```
scheduler/    调度库（时钟、事件、作业、EDF 调度器）及单元/验收测试
httpapi/      本地 JSON HTTP 接口及集成测试
cmd/server/   可执行入口（真实时钟 + SleepExecutor 演示执行器）
examples/     requests.sh 请求样例脚本
```

## 核心语义

### 保守准入（可手算）

提交时对「运行中作业的剩余声明上界 + 队列中作业 + 候选作业」按截止时间排序做 EDF 模拟，
从 `now + 运行中作业剩余上界` 开始累加各作业声明上界，**任一**模拟完成时间超过其截止时间即拒绝。
该判据是保守的（充分不必要）：它假设运行中作业必定用满声明上界。

手算任务集（t0 时刻提交，机器空闲，测试 `TestAdmissionHandComputable` 逐步验证）：

| 作业 | 声明上界 | 截止时间 | EDF 模拟 | 结果 |
|------|---------|----------|----------|------|
| J1 | 10s | t0+30s | — | 准入 |
| J2 | 15s | t0+20s | J2 完 15≤20，J1 完 25≤30 | 准入 |
| J3 | 10s | t0+24s | J2 完 15≤20，**J3 完 25>24** | **拒绝（预计不可满足）** |
| J4 | 5s  | t0+40s | J2 15，J1 25，J4 完 30≤40 | 准入 |

### 状态机

```
SUBMIT 后二选一：
  REJECTED_INFEASIBLE（终态，未占用资源）
  QUEUED → RUNNING → COMPLETED   （截止判定：finished_at ≤ deadline → deadline_met）
                   → TIMEOUT     （实际执行超过声明上界；计入 deadline_missed）
  QUEUED → CANCELLED（排队取消，不触碰执行槽）
  RUNNING → CANCELLED（取消运行中作业；槽位由 worker 唯一出口释放一次）
```

### 资源守恒

机器抽象为 1 个执行槽。不变量：**静止时 `slot_acquired == slot_released` 且 `slot_in_use == false`**。
释放只发生在 `runJob` 的唯一出口；取消路径（排队/运行中/重复取消）均不直接释放。
重复取消返回错误（HTTP 409），不产生任何事件或释放。

### 边界说明

声明上界是硬超时：定时器在 `started_at + exec_bound` 触发。若作业实际执行**恰好**等于声明上界，
完成与超时同时就绪，结局由运行时选择（可能记为 TIMEOUT）。这是保守且安全的解释：
声明上界的含义是"保证不超过"，压线即视为违约风险。演示样例中 `long` 作业
`simulate_actual_ms` 故意小于 `exec_bound_ms` 以避免此歧义。

## HTTP 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/jobs` | 提交作业。201 准入；422 预计不可满足；400 参数非法 |
| GET  | `/jobs` | 全部作业（运行中/排队/已终结） |
| GET  | `/jobs/{id}` | 单个作业 |
| POST | `/jobs/{id}/cancel` | 取消。200 成功；404 不存在；409 已终结/重复取消 |
| GET  | `/events` | 结构化事件流（seq 递增） |
| GET  | `/stats` | 计数器：提交/准入/拒绝/完成/超时/取消/截止达成与错失/槽位获取与释放 |

提交体：

```json
{
  "id": "quick",                  // 可选，缺省自动生成
  "exec_bound_ms": 200,           // 声明执行上界（必填，>0）
  "deadline_in_ms": 30000,        // 相对提交时刻的截止时间（必填，>0）
  "simulate_actual_ms": 50        // 可选，仅演示执行器使用：模拟实际执行时长
}
```

`simulate_actual_ms` 只被演示用的 `SleepExecutor` 读取，用于在真实服务器上模拟
"实际执行超出声明"的场景；调度器本身从不读取它。

## 运行

```bash
go build -o server ./cmd/server
./server -addr 127.0.0.1:8080
# 另一终端：
./examples/requests.sh 127.0.0.1:8080
```

## 请求样例与真实输出

以下为本机实际运行（`./server -addr 127.0.0.1:38081`，2026-09-23）的输出摘录。

提交快速作业并完成：

```
$ curl -s -X POST localhost:38081/jobs -d '{"id":"quick","exec_bound_ms":200,"deadline_in_ms":30000,"simulate_actual_ms":50}'
{"job":{"id":"quick","state":"QUEUED","exec_bound_ms":200,"deadline":"2026-09-23T21:08:45.020238343+08:00",...}}
$ curl -s localhost:38081/jobs/quick
{"job":{"id":"quick","state":"COMPLETED",...,"finished_at":"2026-09-23T21:08:15.070662042+08:00","deadline_met":true}}
```

预计不可满足 → 422（长作业占住机器后）：

```
$ curl -s -X POST localhost:38081/jobs -d '{"id":"hopeless","exec_bound_ms":1500,"deadline_in_ms":500}'
{"error":"infeasible","job":{"id":"hopeless","state":"REJECTED_INFEASIBLE",...}}
```

排队取消与重复取消：

```
$ curl -s -X POST localhost:38081/jobs/queued/cancel
{"job":{"id":"queued","state":"CANCELLED",...}}
$ curl -s -o /dev/null -w '%{http_code}' -X POST localhost:38081/jobs/queued/cancel
409
```

执行超出声明上界 → TIMEOUT（与准入拒绝不同的终态）：

```
$ curl -s -X POST localhost:38081/jobs -d '{"id":"overrun","exec_bound_ms":300,"deadline_in_ms":60000,"simulate_actual_ms":5000}'
$ curl -s localhost:38081/jobs/overrun
{"job":{"id":"overrun","state":"TIMEOUT",...,"finished_at":"2026-09-23T21:08:17.983031307+08:00","deadline_met":false}}
```

最终统计（截止时间统计 + 资源守恒）：

```
$ curl -s localhost:38081/stats
{"submitted":5,"admitted":4,"rejected_infeasible":1,"completed":2,"timed_out":1,
 "cancelled":1,"deadline_met":2,"deadline_missed":1,
 "slot_acquired":3,"slot_released":3,"slot_in_use":false}
```

（5 个提交中：quick/long 完成且达成截止，hopeless 准入拒绝，queued 排队取消，
overrun 运行超时计入错失；`slot_acquired == slot_released == 3` 且槽位空闲，资源守恒成立。
另一次运行曾让 `long` 的 `simulate_actual_ms` 恰好等于 `exec_bound_ms`，压线被记为
TIMEOUT——见上文"边界说明"。）

事件流（节选）：

```
$ curl -s localhost:38081/events
{"events":[
  {"seq":1,"time":"...","job_id":"quick","type":"submitted","detail":{"exec_bound":"200ms",...}},
  {"seq":2,"time":"...","job_id":"quick","type":"admitted"},
  {"seq":3,"time":"...","job_id":"quick","type":"slot_acquired"},
  {"seq":4,"time":"...","job_id":"quick","type":"started"},
  {"seq":5,"time":"...","job_id":"quick","type":"slot_released"},
  {"seq":6,"time":"...","job_id":"quick","type":"completed",...},
  {"seq":7,"time":"...","job_id":"quick","type":"deadline_met"},
  ...]}
```

## 测试

```bash
go vet ./...
go test -race -count=1 -v ./...
```

覆盖（`scheduler/scheduler_test.go`、`httpapi/server_test.go`）：

- 手算任务集的准入判定（J1–J4，见上表）
- 超时与准入拒绝的区分：实际 25s、声明 10s 的作业在 t0+10s 被停止，状态 TIMEOUT，
  计入 `deadline_missed`，槽位恰好获取/释放各一次
- 排队取消：不启动、不触碰槽位；重复取消报错且不产生额外事件
- 运行中取消：槽位恰好释放一次；执行器确实观察到 ctx 取消
- HTTP 层：201/422/409/404 状态码、统计与事件端点、槽位守恒

### 实际运行记录（如实）

| 轮次 | 命令 | 结果 |
|------|------|------|
| 1 | `go test -race -count=1 -v ./...` | **未通过**：1 个数据竞争（`Submit` 把内部 `*Job` 直接交给 HTTP 层读取）；3 个测试缺陷（被拒作业未入 `finished` 致 `Get` 查不到；同截止时间作业 LIFO 插队；超时定时器注册晚于状态可见，手动时钟下可能错过推进点） |
| 2 | 修复后 `go test -race -count=1 ./...` | 通过 |
| 3 | `go test -race -count=5 ./...` | **暴露残余 flake**：执行器 goroutine 异步注册定时器，测试可能在其注册前推进时钟。修复：`ManualClock.Pending()` + 测试等待定时器就绪 |
| 4 | `go test -race -count=10 ./scheduler/ ./httpapi/` | **全部通过**（含竞态检测） |
| 5 | 真实服务器 + `examples/requests.sh` 全部 curl 样例 | 通过；发现 `simulate_actual_ms == exec_bound_ms` 压线时记 TIMEOUT，已写入"边界说明" |

## 已知限制

- 单机单槽位，非抢占；多机/多资源不在范围内
- 准入判据保守（充分不必要）：可能拒绝实际可调的作业，绝不准入不可保证的作业
- 事件日志与作业状态均在内存中，重启即丢失
- 取消运行中作业依赖执行器响应 ctx 取消；不响应的执行器会拖延槽位释放
  （`Stop` 同样等待当前作业结束）
