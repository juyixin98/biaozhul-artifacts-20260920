# fairdrf — 多资源公平调度（两维加权 DRF）纯后端

一个用 Go 实现的可测试后端调度库 + 本地 HTTP 接口，对 **CPU 与内存两维资源**做
Dominant Resource Fairness（DRF）调度。任务**不可抢占**；资源不足时任务在
租户 FIFO 队列中等待。调度时钟（`Clock`）与执行器（`Executor`）均为可替换接口，
所有状态变更写入**结构化事件日志**。无前端。

- 语言：Go 1.22，仅标准库
- 入口：`cmd/fairdrf`（真实时钟的 HTTP 服务）、`cmd/drfsim`（假时钟确定性回放）
- 核心库：`scheduler/`（无第三方依赖，可被任意 Go 程序直接引用）

---

## 1. 调度模型与规则

### 1.1 资源单位

整数计量，杜绝浮点累计误差：

| 维度 | 字段 | 单位 |
|---|---|---|
| CPU | `cpu_milli` | 毫核，1000 = 1 个核 |
| 内存 | `mem_mib` | MiB |

集群容量在启动时固定（`-cpu-milli`、`-mem-mib`）。已用资源在两个维度上
**永不超过容量**（不超配，由实现与测试共同保证）。

### 1.2 租户、权重与主导份额

每个租户注册时必须给出**正整数权重** `weight`（缺省由调用方显式提供，不提供
则 API 拒绝；默认语义为 1）。

对租户 `i`，其**主导份额（dominant share）**为两维份额的较大者：

```
ds_i = max( allocatedCPU_i / capacityCPU,  allocatedMem_i / capacityMem )
```

调度决策比较的是**加权主导份额**：

```
score_i = ds_i / weight_i
```

`score` 越小越优先——这正是加权 DRF：权重为 2 的租户在同等竞争下可获得约
2 倍于权重 1 租户的主导资源份额。份额比较用 `math/big` 整数精确运算
（交叉相乘，不做浮点比较），因此排序结果与资源量级无关、可重现。

### 1.3 确定性平局规则

当两个租户在同一次决策中完全可比时，按以下顺序打破平局（全部确定）：

1. 加权主导份额较小者优先（精确有理数比较）；
2. 仍相同 → 当前 **running 任务数较少**者优先；
3. 仍相同 → **租户 ID 字典序**较小者优先。

不使用 Go map 迭代顺序、不使用 goroutine 到达顺序；测试
`TestDeterministicTieBreak` 用两次不同注册/提交顺序的相同负载验证结果一致。

### 1.4 排队、非抢占与队头规则

- 每个租户一个 **FIFO 等待队列**。调度器只考虑各租户的**队头任务**。
- 一个任务一旦 `RUNNING` 就占用其申请量直到执行器报告完成，**任何情况下
  都不会被驱逐或缩减**（不可抢占）。
- 每次"提交任务"或"任务完成"后触发一次调度过程：把队头可行的租户按
  §1.3 排序，**从最低份额租户开始**，只要其队头在当前可用余量内放得下就启动；
  每启动一个任务立即重新排序，直到没有任何队头能放入当前余量。
- 若高优先租户的队头在**当前余量**下放不下，调度器会**跳过它继续尝试后续
  租户**中放得下的小任务（释放资源只会发生在任务完成时，单轮内等待不会
  变得更有利）。
- **永久阻塞**：若队头任务的申请量超过集群总容量（哪怕集群全空也放不下），
  它永远无法启动，并因此阻塞**本租户**队列其后的任务（FIFO 队头阻塞）；
  但**绝不影响其他租户**——其他租户照常调度。该状态：
  - 提交时写入 `TASK_BLOCKED` 事件；
  - 在 `/snapshot` 中以 `head_blocked` 与 `head_exceeds_capacity` 标记。

### 1.5 为什么任务声明 duration

真实系统里任务结束由执行器上报。为了让单文件演示可独立运行，内置
`TimedExecutor` 在任务启动后按 `duration` 自动回调完成。`Executor` 是接口，
可替换为"等外部信号再 complete"的执行器；`Clock` 同理可替换为假时钟，
因此同一段调度逻辑既跑在墙钟上，也能在测试里做逐秒确定性模拟。

### 1.6 事件（结构化状态记录）

每次状态变化向 `EventStore` 追加一条事件（内存实现，有序号
`event_id`，从 1 开始单调递增）：

| 类型 | 时机 |
|---|---|
| `TENANT_CREATED` | 注册租户 |
| `TASK_SUBMITTED` | 任务进入等待队列 |
| `TASK_BLOCKED` | 任务申请量超过集群总容量（永久不可行） |
| `TASK_STARTED` | 任务获得资源、开始执行 |
| `TASK_FINISHED` | 执行器报告完成、资源归还 |

每条事件都带：`event_id`、`seq`（全局提交序号）、`type`、`at`（时钟时间）、
`tenant_id`、`task_id`、`request`、以及变更后的集群 `used_after` /
`available_after`，便于审计、重放与外部计费。可通过 `GET /events?after=<id>&limit=<n>`
增量拉取。

---

## 2. 手算验收：资源分配序列（重点）

容量 **10 CPU / 10 MiB**（抽象整数单位），三个**等权（weight=1）**租户
A、B、C。任务记为 `ID 租户(CPU,Mem) 时长`：

| 提交时刻 | 任务 |
|---|---|
| t=0 | T1 A(6,1)d2，T2 A(6,6)d3，T3 B(4,9)d5，T4 C(9,9)d5，T5 B(1,1)d1，T6 B(2,2)d3，T7 C(1,1)d3 |
| t=1 | T8 B(1,1)d1（小任务在集群满载时持续到达） |

### 逐步推导

**t=0，一批提交：**

1. T1 提交时集群空 → 直接启动。已用 (6,1)，余量 (4,9)。
2. T2 是 A 的队头，需 (6,6)，CPU 只剩 4 → 等待。
3. T3 是 B 的队头，需 (4,9)，余量 (4,9) 恰好够 → 启动。已用 (10,10)，余量 (0,0)。
4. T4 C(9,9)：全集群才 (10,10)，当前余量 0 → 等待。
5. T5/T6：B 的队头 T3 已 running，T5 成为队头，但集群满 → 等待。
6. T7：C 的队头是 T4，T4 没启动，FIFO 下 T7 等待。

> 此时 running：T1(A)、T3(B)。注意 DRF 选择 T3 而非 T2：
> T3(4,9) 两维都放进余量 (4,9)，而 T2 要 6 CPU 放不进。这就是"大任务
> 被小/合适任务越过"的资源形状约束。

**t=1：** T8 到达，集群满 → WAITING（小任务持续到达也不抢占）。

**t=2：** T1 完成，释放 (6,1)，余量 (6,1)。

- 候选队头：A 的 T2 需 (6,6) → Mem 1 < 6，放不下；
  B 的 T5 需 (1,1) → 放得下；C 的 T4 需 (9,9) → 放不下。
- 跳过 A、C，**T5 启动**。已用 (5,10)，余量 (5,0)。
- 大任务 T2 仍被内存卡住。

**t=3：** T5 完成，释放 (1,1)，余量 (6,1)。T2 需 6 Mem、T4 需 9/9、
B 新队头 T6 需 2 Mem，全部 > 1 Mem → 无人启动。

**t=4：** 无任务到期，不变。

**t=5：** T3 完成，释放 (4,9)，余量 (10,10)。此时三个租户加权份额都为 0
（A 此前的 T1 已结束，B/C 无 running），平局按 running 数（都 0）再按
租户 ID 字典序：

- 第一轮：A < B < C → **T2 启动** (6,6)，余量 (4,4)；
- 重新排序：B、C 份额 0 低于 A → B 队头 T6(2,2) → **T6 启动**，余量 (2,2)；
- 重新排序：B 份额 0.2、C 份额 0 → C 队头 T4 需 (9,9) 放不下，跳过；
  B 下一任务 T8(1,1) 成为 B 队头且放得进 (2,2) → **T8 启动**，余量 (1,1)；
- C 的 T4(9,9) 仍放不下，T7 受同租户 FIFO 队头阻塞。

已用 (9,9)。

**t=6：** T8（d=1）完成，释放 (1,1)，余量 (2,2)。

- A 无等待；C 队头仍是 T4(9,9)，放不下 → **T7 受自己租户队头 T4 阻塞**，
  虽然 (1,1) 对 T7 本身足够。这演示"同租户队头阻塞"。
- 已用回到 (8,8)。

**t=7：** 无到期，不变。

**t=8：** T2（d=3，t=5 起）与 T6（d=3，t=5 起）同一截止时刻；
假时钟按定时器**插入顺序**触发，T2 先完成：

- T2 释放 (6,6)，余量 (8,8)：T4(9,9) 仍差一点，放不下；
- 同一时刻 T6 再释放 (2,2)，余量 (10,10) → **T4 启动** (9,9)，余量 (1,1)；
- C 的下一任务 T7 成为队头，(1,1) 恰好放进剩余 → **T7 在同一调度轮启动**。

已用 (10,10)。

**t=11：** T7（t=8 起，d=3）完成，已用 (9,9)；T4 继续占资源（不可抢占）。

**t=13：** T4（t=8 起，d=5）完成，集群 (0,0)，全部任务 FINISHED。

### 结论序列（与实现、测试、模拟三者一致）

```
启动顺序 : T1, T3, T5, T2, T6, T8, T4, T7
完成顺序 : T1, T5, T3, T8, T2, T6, T7, T4
```

该序列由 `scheduler/acceptance_test.go::TestAcceptanceHandCalculated`
逐秒断言（含每一步 used/available 与每任务状态），并可由
`go run ./cmd/drfsim` 复现，完整事件流见
[`examples/acceptance-events.jsonl`](examples/acceptance-events.jsonl)。

---

## 3. HTTP 接口

服务默认监听 `:8080`，容量默认 10000 milliCPU / 10240 MiB：

```bash
go run ./cmd/fairdrf -addr :8080 -cpu-milli 10000 -mem-mib 10240
```

| 方法与路径 | 说明 |
|---|---|
| `GET  /healthz` | 健康检查 |
| `POST /tenants` | 创建租户，body `{"id","weight"}` |
| `GET  /tenants` | 租户列表（含份额、running/waiting） |
| `GET  /tenants/{id}` | 单个租户 |
| `POST /tasks` | 提交任务，body 见下；返回提交后任务状态（202） |
| `GET  /tasks` | 全部任务 |
| `GET  /tasks/{id}` | 单个任务 |
| `GET  /snapshot` | 集群一致快照（容量/已用/可用/租户/任务/事件序号） |
| `GET  /events?after=<id>&limit=<n>` | 增量事件 |

任务 body：

```json
{
  "id": "etl-001",
  "tenant_id": "team-analytics",
  "request": { "cpu_milli": 2000, "mem_mib": 2048 },
  "duration": "30s"
}
```

`duration` 为 Go duration 字符串（`"500ms"`、`"10s"`、`"2m"`）。
任务状态为 `WAITING` / `RUNNING` / `FINISHED`。

错误以 JSON `{"error": ...}` 返回，状态码：400（校验失败）、404（租户/任务
不存在）、409（ID 重复）、405（方法不允许）。未知 JSON 字段一律拒绝。

可直接运行演示脚本：[`examples/requests.sh`](examples/requests.sh)。

---

## 4. 目录结构

```
.
├── go.mod
├── README.md
├── scheduler/                  # 核心调度库（可被外部引用）
│   ├── types.go                # 资源/任务/事件类型与错误
│   ├── scheduler.go            # 加权 DRF、平局规则、FIFO、快照
│   ├── clock.go                # Clock: RealClock + 确定性 FakeClock
│   ├── executor.go             # Executor: TimedExecutor（可替换）
│   ├── events.go               # EventStore + 内存实现
│   ├── http.go / http_helpers.go
│   ├── acceptance_test.go      # §2 手算场景逐秒断言
│   ├── scheduler_test.go       # 权重/平局/阻塞/不超配随机压测/校验
│   └── http_test.go            # HTTP 端到端
├── cmd/
│   ├── fairdrf/main.go         # 真实时钟 HTTP 服务
│   └── drfsim/main.go          # 假时钟回放 §2 场景，打印事件 JSONL
└── examples/
    ├── create-tenant.json
    ├── create-tenant-batch.json
    ├── submit-task.json
    ├── submit-task-huge.json   # 超容量（永久阻塞）样例
    ├── requests.sh             # curl 端到端脚本
    └── acceptance-events.jsonl # 手算场景完整事件日志（复现工件）
```

---

## 5. 运行与测试

```bash
# 编译 / vet
go build ./...
go vet ./...

# 全部自动化测试（含 -race）
go test -race -v ./...

# 确定性复现手算场景（输出 JSONL 事件）
go run ./cmd/drfsim

# 启动 HTTP 服务
go run ./cmd/fairdrf -addr :8080 -cpu-milli 10000 -mem-mib 10240
```

测试覆盖：

- **手算验收序列**：逐秒断言 used/available、每个任务状态、启动/完成全序，
  并在**每个事件后**检查不超配；
- **加权 DRF**：weight 1:2 在争用下稳态为 4:6，且接纳次序严格匹配推导；
- **确定性平局**：等份额/等 running 数下按租户 ID，且不同注册/提交顺序
  两次运行结果完全一致；
- **大任务阻塞**：超容量队头只阻塞本租户、永不波及其他租户，并发出
  `TASK_BLOCKED`；同租户 FIFO 队头阻塞单独覆盖；
- **小任务持续到达**：满载时到达的小任务正确等待、资源释放后被接纳；
- **资源释放与复用、不可抢占**；
- **随机 400 轮混合负载不超配**：每步核对 used ≤ capacity、租户分配量
  等于其 running 之和、各租户之和等于集群 used，排空后无"可行任务被遗留"；
- **HTTP 端到端**与参数/方法/状态码校验。

---

## 6. 已知边界与设计取舍

- 单一集群、固定二维容量；未做多机分片、无持久化（`MemoryStore` 进程内）。
  `EventStore` 接口可替换为持久实现做重放。
- 任务为"申请多少就全程独占多少"的刚性刻画，不支持任务运行中变更需求；
  不可抢占是题目明确要求。
- 超容量任务永久停留在本租户队头（FIFO 队头阻塞）。这是显式的语义选择：
  它不会阻塞其他租户，但其后同租户任务需要运维侧取消该任务才能继续。
  当前未提供取消/删除任务的 API（YAGNI），如需要可在 `Scheduler` 上
  增加 `Cancel` 并补 `TASK_CANCELLED` 事件。
