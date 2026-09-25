# 多资源公平调度器（DRF Scheduler）

纯后端的多租户任务调度库 + 本地 HTTP 接口。在 **CPU、内存两维资源**上实现
**Dominant Resource Fairness（DRF，主导资源公平）**，任务**不可抢占**，资源
不足时排队等待；支持显式租户权重与完全确定性的平局规则；调度时钟与执行器
均可替换；所有状态变更输出结构化事件。

- 语言：Go 1.22，仅标准库（无第三方依赖）
- 入口：`cmd/drfscheduler`
- 库：`pkg/scheduler`（调度核心、时钟、执行器、事件）
- HTTP：`pkg/httpapi`

---

## 1. 调度语义

### 1.1 资源与主导份额

- 资源单位：CPU 用**毫核**（`cpu_millicpu`，1000 = 1 核），内存用 **MiB**
  （`memory_mib`）。全部为整数，份额比较用精确整数运算（int64 快路径 +
  溢出时自动回退 `math/big`），**没有浮点误差**。
- 某租户对资源 *r* 的份额 = 该租户正在运行任务占用的 *r* 之和 / 集群 *r* 容量。
- **主导份额（dominant share）** = max(CPU 份额, 内存份额)。
- **加权主导份额** = 主导份额 / 租户权重。权重越大，长期分得越多。

### 1.2 选择规则（每个调度轮次）

1. **只看每个租户 FIFO 队列的队首**（租户内严格先进先出，队首不满足时后面
   的小任务不得插队，事件原因记为 `TENANT_QUEUE_FULL`）。
2. 队首任务必须在 **CPU 与内存两维都放得进当前空闲资源**（`used + request
   <= capacity`）。放不下的队首本轮跳过（事件原因 `INSUFFICIENT_RESOURCES`）。
3. 在所有放得下的租户中，选择**当前加权主导份额最小**的租户启动其队首。
   > 比较的是租户“当前”的份额；任务请求量只用于可行性判断。这是经典 DRF
   > （Ghodsi 等）的规范做法。
4. **确定性平局**：加权主导份额完全相等时，先比租户 ID（字典序小者胜），
   再比任务 ID。零份额（所有租户都还没拿到资源）也走该规则，因此从零开始
   的启动顺序完全可复现。
5. 启动后回到第 1 步继续选择，直到没有任何队首能放下为止。

### 1.3 不可抢占与等待

- 运行中的任务**永不被驱逐**；新任务只能使用当前空闲资源。
- 缩小集群容量时，若新容量小于正在运行任务的占用，请求被拒绝（422）。
- 任务请求超过集群总容量在提交时即被拒绝（422），不会进入队列。
- 完成 / 取消 / 容量变更会立即触发新一轮调度。

### 1.4 公平性手算示例

集群容量 **9 CPU、18 GB**。租户 A 的任务要 `<1 CPU, 4 GB>`（每份主导 = 内存
2/9），租户 B 的任务要 `<3 CPU, 1 GB>`（每份主导 = CPU 3/9）。权重均为 1，
7 个任务在同一时刻到达时的启动顺序：

| 步骤 | 启动 | 原因（启动前份额） | 启动后 A/B 主导份额 |
|---|---|---|---|
| 1 | A1 | 双方 0/0 平局 → 租户 ID A | A 2/9, B 0 |
| 2 | B1 | B 0 < A 2/9 | A 2/9, B 3/9 |
| 3 | A2 | A 2/9 < B 3/9 | A 4/9, B 3/9 |
| 4 | B2 | B 3/9 < A 4/9 | A 4/9, B 6/9 |
| 5 | A3 | A 4/9 < B 6/9 | A 6/9, B 6/9 |

随后 A4 会让集群 CPU 达到 10 > 9，B3 达到 12 > 9，二者**等待**。释放后
（B2 先结束）份额落后的 B 先补 B3，A 的任务在 A 系任务释放后补入。完整
时间线见 `TestClassicDRFHandComputed`。

权重示例：容量 10/10、任务均为 `<1,1>`、A 权重 1、B 权重 2，精确顺序为
`A1 B1 B2 A2 B3 B4 A3 B5 B6 A4`，最终 4:6（恰为 1:2 权重比）。
见 `TestWeightedDRF`。

---

## 2. 可替换的时钟与执行器

- 时钟接口 `scheduler.Clock`（`pkg/scheduler/clock.go`）
  - `RealClock`：挂墙时间 + 真实定时器（默认）。
  - `FakeClock`：手工 `Advance(d)` 推进的虚拟时钟。推进到某时刻与触发该时刻
    的定时器对外是**原子**的（并发读者看不到“时间已到、完成事件未投递”的
    中间态），因此基于它的测试完全确定性、可重复、零真实等待。
- 执行器接口 `scheduler.Executor`（`pkg/scheduler/executor.go`）
  - `SimExecutor`：任务“运行”其声明的时长，配合 FakeClock 瞬时完成。
  - `ProcessExecutor`：把任务 `spec` 作为 `sh -c` 子进程执行，通过环境变量
    `DRF_TASK_ID / DRF_TENANT_ID / DRF_CPU_MILLICPU / DRF_MEMORY_MIB` 传入
    身份与配额（真实执行需 `-mode=real -executor=process`）。

---

## 3. 结构化事件

每次状态变更产生一条 `Event`（`seq` 单调递增，带虚拟/真实时间戳）：

- `TASK_SUBMITTED`：任务入队
- `TASK_STARTED`：任务交给执行器，含权重、主导份额%、启动后占用
- `TASK_WAITING`：排队未启动，含原因
  （`INSUFFICIENT_RESOURCES` / `TENANT_QUEUE_FULL` / `OVER_CAPACITY`）
- `TASK_FINISHED`：成功 / 失败 / 取消，含结束后占用、运行时长

事件默认写入内存（`GET /v1/events`），可用 `-events-log=path` 额外以
**JSON Lines** 追加落盘（每条立即 flush，可 `tail -f`）。

---

## 4. 构建与运行

```bash
go build ./...
go test ./...            # 全部自动化测试
go vet ./...

# 仿真模式（默认）：假时钟 + 模拟执行器，用 HTTP 推进虚拟时间
go run ./cmd/drfscheduler -addr 127.0.0.1:8080 \
    -capacity-cpu 10000 -capacity-mem 10240 -mode sim
```

真实执行模式：

```bash
# 挂墙时钟 + 模拟时长（按真实秒数等待）
go run ./cmd/drfscheduler -mode=real -executor=sim

# 挂墙时钟 + 真实子进程（任务体为 shell 命令）
go run ./cmd/drfscheduler -mode=real -executor=process
```

参数：`-addr`、`-capacity-cpu`、`-capacity-mem`、`-mode sim|real`、
`-executor sim|process`（process 仅 real）、`-events-log <path>`。

---

## 5. HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 健康检查与运行模式 |
| GET | `/v1/state` | 全量快照：容量/已用/空闲、每租户份额、任务、不超配校验 |
| PUT | `/v1/cluster/capacity` | 修改集群容量（低于当前占用会 422） |
| PUT | `/v1/tenants/{id}` | 创建/更新租户权重（body `{"weight": 2}`） |
| GET | `/v1/tenants` | 列出租户及其精确份额 |
| POST | `/v1/tasks` | 提交单个任务（202；超过容量 422；未知租户 404） |
| POST | `/v1/tasks/batch` | 批量提交：整批视为同一时刻到达，先做一次调度（手算场景）；任一元素非法则整批回滚 |
| GET | `/v1/tasks?tenant_id=&state=` | 列出/过滤任务 |
| GET | `/v1/tasks/{id}` | 任务详情 |
| POST | `/v1/tasks/{id}/cancel` | 取消排队任务（运行中尽力终止进程） |
| GET | `/v1/events` | 最近结构化事件 |
| POST | `/v1/schedule` | 手动触发一轮调度 |
| POST | `/v1/clock/advance` | **仅仿真模式**：推进虚拟时钟，body `{"advance_ms": 100}` |

### 请求体

任务：

```json
{
  "tenant_id": "A",
  "cpu_millicpu": 1000,
  "memory_mib": 4000,
  "duration_ms": 100,
  "id": "A1"
}
```

`id` 可省略（自动生成）。仿真模式用 `duration_ms`；进程模式可用 `spec`
传 shell 命令。详见 `examples/` 下的样例与 `scripts/demo.sh`。

---

## 6. 验收场景与自动化测试

`go test ./...` 覆盖（全部通过，见 `RUNLOG.md` 实际记录）：

- `TestClassicDRFHandComputed`：上表手算序列 + 释放后补调度，逐步断言。
- `TestBigTaskBlocksSmallTasks`：占满集群的大任务阻塞两个小任务（
  `INSUFFICIENT_RESOURCES`），大任务释放后两小任务按租户序启动。
- `TestSmallTasksArrivingContinuously`：集群满载时小任务持续到达，
  t=5ms、t=10ms 释放点上“份额最落后者优先”。
- `TestWeightedDRF`：1:2 权重的精确启动序列与 4:6 结果、精确分数份额。
- `TestPerTenantFIFOHeadBlocking`：同租户队首阻塞（`TENANT_QUEUE_FULL`）。
- `TestMemoryDominantDimension`：内存成为主导维时的选择。
- `TestDeterministicReplay`：相同输入重放，顺序逐位一致。
- `TestRandomizedNoOvercommitAndDRF`：12 个随机种子 × 300 步随机到达/释放，
  全程断言两维不超配、租户 FIFO、最终全部完成且占用归零（同步假时钟驱动）。
- `clock_test.go`：假时钟的截止期顺序、同时刻注册序、提前不触发、Stop、
  在回调中再注册定时器。
- `pkg/httpapi`：端到端 HTTP 场景、错误码（404/422/400）、容量伸缩拒绝、
  实时模式禁止推进时钟。

“不超配”在测试中以两种方式校验：重放事件流累计 `used`，以及直接读取快照
的 `invariant_check.used_fits_capacity`。

---

## 7. 目录

```
cmd/drfscheduler/      HTTP 服务入口
pkg/scheduler/         调度核心、时钟、执行器、事件、状态视图
pkg/httpapi/           HTTP 路由与处理器
examples/              curl 请求样例（.http / .sh）
scripts/demo.sh        一键端到端演示（仿真模式）
RUNLOG.md              实际构建/测试/运行命令与结果记录
```
