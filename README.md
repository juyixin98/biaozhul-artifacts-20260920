# deadlineadm — 截止时间准入调度库（纯后端）

单机、非抢占、最早截止期优先（EDF）的作业调度库，带**保守准入控制**：
任务在提交时**声明执行时间上界**，调度器通过模拟 EDF 调度判断该任务是否
可能满足截止期，不可满足则在**运行前**拒绝；运行时若实际执行超过声明上界，
则作为**运行超时**单独处理。时钟与执行器均可替换，所有状态变更输出为结构化
事件记录。纯 Go 后端，无任何前端。

- 语言：Go 1.22，仅标准库（无第三方依赖）
- 模块：`deadlineadm`

## 它解决的问题与关键区分

1. **预计不可满足（predicted infeasible）**：提交时用已有未完成任务 + 候选任务
   做一次 EDF 可行性模拟；若存在无法在截止期前完成的任务，立即拒绝
   （HTTP 422，事件 `rejected`）。该任务**不占用任何资源、从不运行**。
2. **运行超时（runtime timeout）**：任务声称的执行上界被实际执行超过。
   - 默认策略 `kill_at_budget`：在声明上界处杀死任务，计为 `timeout`，保护后续任务。
   - 备选策略 `observe`：允许继续到截止期才杀死，迟于截止期完成计为
     `deadline_missed`，并可能级联拖垮后续任务。
3. **取消不重复释放资源**：排队任务取消时它从未获取资源，故不释放任何资源；
   运行中任务取消后，资源只在执行器确认终止时**恰好释放一次**（actor 串行 +
   `released` 守卫，重复释放会 panic）；对已终止任务再次取消返回错误且无副作用。

## 架构

| 包 | 职责 |
|---|---|
| `clock` | 可替换时钟：`RealClock`（挂钟）与 `FakeClock`（手动驱动的确定性时钟，测试/demo 用） |
| `event` | 结构化事件：`Event{seq,time_ms,type,job_id,reason,detail}`；内存 sink、JSONL sink、多路 sink |
| `executor` | 可替换执行器：`ScriptExecutor`（时钟驱动的 `sleep:<ms>[,fail]`，测试/demo）与 `CommandExecutor`（`sh -c` 本地进程，HTTP 服务可选） |
| 根包 `deadlineadm` | EDF 堆、保守准入模拟 `SimulateEDF`、单 actor 调度器（EDF 派发、容量账本、取消、超时、统计）、`FakeDriver` |
| `httpserver` | 本地 HTTP/JSON 接口 |
| `cmd/server` | HTTP 服务入口 |
| `cmd/demo` | 三组可手算任务集的确定性演示 |

### 为什么正确性可保证

- **单 actor**：所有可变状态（队列、运行集合、资源账本）只在一个 goroutine 上
  修改，公共 API 通过 channel 投递消息，因此获取/释放天然无竞态。
- **EDF 同构**：准入模拟 `SimulateEDF` 与运行时 `pump` 使用同一条派发规则
  （截止期升序、同刻 FIFO、队首阻塞、容量内并行）。
- **界事件与执行解耦**：运行任务的执行上下文只由调度器内部定时事件
  （预算界/截止期）或显式取消来取消；自然完成先解除界事件，因此“恰好在界点
  完成”的诚实任务不会被误杀。
- **可替换时钟/执行器**：调度器只依赖 `clock.Clock` 与 `executor.Executor`
  接口。

## 构建与测试

```bash
go build ./...
go vet ./...
go test ./...            # 全部自动化测试
go test -race ./...      # 竞态检测（已验证通过）
go run ./cmd/demo        # 三组可手算任务集，打印事件时间线/统计/资源账本
```

无外部依赖，`go test` 不需要联网。

## 运行本地 HTTP 服务

```bash
# 安全、自包含的脚本执行器（推荐用于试用）
go run ./cmd/server -addr 127.0.0.1:8080 -executor script -overrun kill_at_budget \
  -event-log events.jsonl

# 或执行真实本地 shell 命令（仅绑定可信网络！）
go run ./cmd/server -addr 127.0.0.1:8080 -executor command
```

参数：`-capacity`（资源单位数，默认 1）、`-overrun=kill_at_budget|observe`、
`-executor=script|command`、`-event-log`（可选 JSON Lines 落盘）。

### HTTP 接口

| 方法与路径 | 说明 |
|---|---|
| `POST /jobs` | 提交任务；准入失败返回 422，校验错误 400，成功 202 |
| `GET /jobs` | 全部任务快照 |
| `GET /jobs/{id}` | 单个任务 |
| `POST /jobs/{id}/cancel` | 取消排队/运行中任务；已终止 409，未知 404 |
| `GET /stats` | 计数 + 截止期统计 + 资源守恒不变量 |
| `GET /events[?job_id=]` | 结构化事件记录 |
| `GET /healthz` | 存活检查 |

提交 body（时间均为整数毫秒；`deadline_rel_ms` 相对服务器当前时间，
也可用绝对 `deadline_ms`）：

```json
{ "id": "a", "payload": "sleep:300", "demand": 1,
  "deadline_rel_ms": 2000, "budget_ms": 300 }
```

字段含义：`budget_ms` 是**声明的执行时间上界**；`demand` 是运行时占用的资源
单位（默认 1，不能超过机器容量）；`payload` 对调度器不透明，交给执行器。

完整 curl 流程见 [`examples/requests.sh`](examples/requests.sh)，真实抓取的
响应见 [`examples/sample_responses.md`](examples/sample_responses.md)。

## 可手算任务集与验收

`go run ./cmd/demo` 输出三组场景，均可手工核对：

### 场景 1：EDF + 保守准入（预计不可满足）
单机、均在 t=0 到达：

| 任务 | 声明上界 budget | 截止期 | 实际 |
|---|---|---|---|
| j1 | 30 | 100 | 30 |
| j2 | 50 | 80 | 50 |
| j3 | 20 | 120 | 20 |
| j4 | 40 | 110 | 40（提交即被拒） |

EDF 手算：j1 `[0,30]`、j2 `[30,80]`、j3 `[80,100]` 均满足。加入 j4 后不存在
能让四个任务都按时完成的 EDF 顺序，故准入在**提交时**拒绝 j4（事件
`rejected`，原因指出首先无法满足的任务），j4 不运行。

### 场景 2：超出声明 = 运行超时（区别于截止期错过）
单机：`bad` 声明 20、截止期 100，实际运行 60；`a`、`b` 各声明 30，截止期
120、200。默认 `kill_at_budget` 下，`bad` 在 t=20 被杀死，计为 `timeout`；
a `[20,50]`、b `[50,80]` 均按时完成。超时与 `deadline_missed` 是两类独立计数。

### 场景 3：排队取消与资源守恒
`long`（声明 120）运行中；`q1`、`q2` 排队。t=5 取消排队的 q1：不释放任何资源；
再次取消返回 `already terminal` 且状态不变；随后取消运行中的 long：其 1 单位
资源在执行器确认终止时**恰好释放一次**，q2 随即运行。最终
`acquired_total == released_total == 2`，`resource_conserved=true`，每个取消的
任务恰有一条 `canceled` 事件。

### 自动化测试覆盖
- `admission_test.go`：EDF 可行性模拟的手算完成时间、过载拒绝、恰等截止期可行、
  多资源并行、队首阻塞、未来释放时间、运行中任务余量。
- `scheduler_test.go`：上面三场景的端到端断言、排队截止期错过、并行资源守恒、
  执行失败释放资源、重复取消幂等、事件顺序与字段。
- `clock/`、`executor/`、`event/`、`httpserver/`：各自的单元/接口测试。

## 资源守恒不变量

`Stats.ResourceConserved` 定义为

```
acquired_total == released_total + in_use
```

即：调度器发放过的每一份资源，要么仍被某运行中任务占用（`in_use`），要么已经
归还（`released_total`）。取消、超时、失败、正常完成四条终止路径都恰好归还一次；
排队任务从不进入账本。所有测试在结束时都断言该不变量为真。

## 目录

```
clock/        可替换时钟（Real / Fake）
event/        结构化事件与 sink
executor/     可替换执行器（Script / Command）
*.go          根包：EDF 堆、准入模拟、调度器 actor、FakeDriver
httpserver/   本地 HTTP/JSON 接口
cmd/server/   HTTP 服务
cmd/demo/     可手算验收演示
examples/     curl 请求脚本与真实响应样例
```
