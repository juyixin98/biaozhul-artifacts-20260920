# pilab — 优先级继承模型（Priority Inheritance Lab）

纯后端的**单处理器可抢占任务 + 互斥锁模拟器**，实现**优先级继承协议（PIP）**，
支持嵌套锁的**传递（多级）优先级继承**、锁释放后的**有效优先级重算/回退**，
并在等待图（wait-for graph）中**检测死锁环**。所有状态变更都以**结构化事件**
记录，可输出完整的**调度时间线 JSON**；同时提供可替换的时钟与执行器、本地 HTTP
接口（含 SSE 事件流）与自动化测试。**本项目不含任何前端。**

- 语言：Go 1.22（仅标准库，无第三方依赖）
- 形态：可作为库引用（`scheduler` 包），也提供 CLI 与本地 HTTP 服务

---

## 1. 目录结构

```
.
├── go.mod
├── scheduler/            # 核心调度库（与传输层、时钟完全解耦）
│   ├── types.go          # 配置/程序/任务/锁的数据模型
│   ├── events.go         # 事件类型、事件结构、报告结构
│   ├── clock.go          # Clock 抽象：SimClock / CountingClock / WallClock
│   ├── executor.go       # Executor 抽象 + 默认 ProgramExecutor
│   ├── scheduler.go      # 调度内核：抢占、PIP、重算、死锁检测
│   └── render.go         # 紧凑文本时间线（演示用）
├── scenario/             # 内置经典场景预设 + 有/无 PIP 量化对比
├── httpapi/              # 本地 HTTP 接口（JSON + SSE）
├── cmd/pilab/            # CLI：serve / scenario / timeline / list / compare / run
└── examples/             # 请求样例、参考输出、真实运行记录
    ├── *.json            # 各场景的可提交配置
    ├── REQUESTS.md       # curl 请求样例
    └── output/           # 实际运行产生的时间线 JSON 与 RUNLOG.txt
```

---

## 2. 模型与语义

### 2.1 任务与程序

- 数字越大优先级越高；每个任务有 `basePriority`（基础优先级）、`arrival`（到达 tick）
  和一段 `program`（指令序列）。
- 指令只有三种：
  - `{"op":"cpu","ticks":N}`：占用处理器执行 N 个 tick；
  - `{"op":"lock","lock":"A"}`：获取互斥锁 A，若被他人持有则阻塞排队；
  - `{"op":"unlock","lock":"A"}`：释放锁 A。
- 锁为**非递归**互斥量；对自己持有的锁再次 `lock` 视为自死锁（报 `fatal`）。
- 任务退出时若仍持有锁，按获取顺序的 **LIFO（嵌套）自动释放**并唤醒等待者。

### 2.2 单处理器抢占

- 任一时刻只有一个任务运行。每个 CPU tick 结束、以及每个零耗时的同步原语操作
  （`lock` 立即可得 / `unlock`）之后都是一个**调度点**：从所有“可运行”任务
  （ready 或正在运行）中选择有效优先级最高者；同优先级按 id 字典序，保证结果确定。
- 更高优先级任务到达会发出 `preempt`，被抢占者回到 ready；没有可运行任务时，
  若等待图无环则把时钟快进到下一个到达时刻（发 `idle` 事件）。

### 2.3 优先级继承（PIP）

- 当任务 X 阻塞在“被 O 持有”的锁上时，形成一条捐赠边 X → O。
- 锁主的**有效优先级 = max(自身基础优先级, 所有直接/间接捐赠者的有效优先级)**。
- 捐赠沿“阻塞—持有”链**传递**：若 O 自己也阻塞在另一把锁上，则 X 的优先级会
  继续传给 O 等待的锁主，从而实现**多级嵌套继承**。内核用捐赠图上的不动点迭代
  统一求解，不硬编码级数。
- 每次图变化（阻塞、唤醒、释放）都会**重建捐赠边并重算全部有效优先级**：
  - 升高发 `priorityBoost`，下降（含回落到基础优先级）发 `priorityReset`；
  - 捐赠关系建立/消失分别发 `donorJoin` / `donorLeave`。
- 因此锁释放后，锁主的有效优先级会按**剩余捐赠者重新计算**（多个等待者时取最高，
  最高者离开后回落到次高，全部离开才回到基础优先级）。
- `inheritance: "none"` 关闭继承（有效优先级恒等于基础优先级），用于复现
  **无界优先级反转**的对照实验。

### 2.4 死锁检测

- 维护有向等待图：**阻塞任务 → 其等待锁的持有者**。
- 每当处理器无可运行任务或发生新的阻塞时，用 **Tarjan 强连通分量 + 分量内环路径
  搜索**找出所有基本环；每个不同的环去重后发一个 `deadlock` 事件（`cycle` 字段
  首尾重复，如 `["P1","P2","P1"]`）。
- 成环后两任务均无法运行释放锁——优先级继承**不能**解环，模拟以
  `deadlocked=true` 终止（这是 PIP 固有局限的反例）。

### 2.5 可替换的时钟与执行器

- `scheduler.Clock`：默认 `SimClock`（整数 tick）。另有 `CountingClock`（统计
  推进量，测试用）与 `WallClock`（把 tick 映射到墙钟）。通过 `WithClock(...)` 注入。
- `scheduler.Executor`：默认 `ProgramExecutor` 顺序解释程序；可实现该接口注入
  脚本化/重放式执行器（见 `scheduler/executor_test.go`），调度策略与程序表示解耦。
- `scheduler.WithEventSink(func(Event))`：事件在写入报告的同时**实时推送**，
  HTTP 的 SSE 端点即基于此实现。

---

## 3. 快速开始

需要 Go 1.22+（本仓库在 `go1.22.2 linux/amd64` 下验证）。无需联网、无第三方依赖。

```bash
go build ./...          # 编译
go test ./...           # 运行全部测试
go run ./cmd/pilab list # 列出内置场景
```

### CLI

```bash
# 运行内置场景，输出完整时间线 JSON
go run ./cmd/pilab scenario inversion-pip

# 紧凑的人类可读文本时间线
go run ./cmd/pilab timeline three-level-inheritance

# 经典反转：有/无 PIP 的量化对比
go run ./cmd/pilab compare

# 运行自定义配置（文件或标准输入）
go run ./cmd/pilab run examples/deadlock-abba.json
cat examples/multi-lock-order.json | go run ./cmd/pilab run -

# 启动 HTTP 服务（默认 :8080）
go run ./cmd/pilab serve -addr :8080
```

### HTTP 接口

| 方法 | 路径 | 说明 |
| ---- | ---- | ---- |
| GET  | `/healthz` | 健康检查 |
| GET  | `/api/scenarios` | 列出内置场景 |
| GET  | `/api/scenarios/<id>` | 查看某场景配置 |
| POST | `/api/scenarios/<id>` | 直接运行内置场景 |
| POST | `/api/simulate` | 提交自定义 `Config` JSON，返回完整报告 |
| POST | `/api/simulate/stream` | 同上，但以 SSE 逐事件推送，末帧为完整 `report` |
| GET  | `/api/compare/inversion` | 有/无 PIP 的对比指标 |

完整 curl 样例见 [`examples/REQUESTS.md`](examples/REQUESTS.md)。

SSE 帧形如：

```
event: event
data: {"tick":4,"seq":16,"type":"priorityBoost","task":"L","oldPrio":1,"newPrio":10,...}

event: report
data: { ...完整 Report... }
```

---

## 4. 验收场景与实测结果

以下数字均来自本仓库代码的真实运行（参考输出在 `examples/output/`，可用
`go run ./cmd/pilab timeline <id>` 复现）。

### 4.1 经典优先级反转（三任务，单锁 A）

- `L` 基础优先级 1，持有 A；`M` 优先级 5（纯计算，到达 t=2）；`H` 优先级 10，
  在 t=4 需要 A（到达 t=3）。

**无继承（`inversion-none`）**——发生无界反转：H 在 t=4 阻塞后，`M` 在 t=4..8
连续运行，直到 t=9 才轮到持锁的 `L`：

```
t=4  block         H   lock=A owner=L
t=4  dispatch      M                 # H 被挡住，M（中优先级）照跑
...
t=9  finish        M
t=12 lockReleased  L                 # L 直到此刻才放出 A
t=13 finish        H                 # 高优先级 H 被拖到 t=13 才完成
   H: blocked=8, finish=13
```

**启用 PIP（`inversion-pip`）**——持锁者继承 H 的优先级，先于 M 完成临界区：

```
t=4  block          H   lock=A owner=L
t=4  donorJoin      L   donor=H lock=A
t=4  priorityBoost  L   1->10         # L 继承 H，M 无法抢占
t=4..6 cpuTick      L   eff=10
t=7  lockReleased   L
t=7  priorityReset  L   10->1         # 释放后重算，回落到基础优先级
t=8  finish         H                 # H 在 t=8 即完成
```

`GET /api/compare/inversion` 的实测汇总：

| 指标 | 无继承 | 启用 PIP |
| ---- | ------ | -------- |
| 高优先级任务 H 完成时刻 | **13** | **8** |
| 节省 tick（`highFinishTickSaved`） | — | **5** |
| 持锁者 L 的优先级提升次数 | 0 | 1（1→10，随后回落到 1） |

### 4.2 三级（嵌套）传递优先级继承（`three-level-inheritance`）

构造一条**非环**捐赠链 `T3(9) → T1(5) → T2(1)`：`T2` 持 B，`T1` 持 A 后等 B，
`T3` 等 A。实测有效优先级沿链传播并在释放时逐级回退：

```
t=2  block          T1  lock=B owner=T2
t=2  priorityBoost  T2  1->5           # T1(5) 捐赠给 T2
t=4  block          T3  lock=A owner=T1
t=4  priorityBoost  T2  5->9           # T3(9) 经 T1 传递到 T2（传递继承）
t=4  priorityBoost  T1  5->9
t=8  wakeup         T1  (T2 释放 B)
t=8  priorityReset  T2  9->1
t=9  wakeup         T3  (T1 释放 A)
t=9  priorityReset  T1  9->5
```

`T2` 的有效优先级完整轨迹为 **1 → 5 → 9 → 1**，证明不是单纯的“一跳”继承。

### 4.3 多锁与释放顺序（`multi-lock-order`）

`L` 以嵌套顺序持有 A、B；`M` 等 B，`H` 等 A。实测：

- t=8 `H` 阻塞在 A 上后，`L` 被提升到 **8**，先把嵌套临界区跑完；
- t=11 先**显式释放 B**（此刻唤醒等 B 的 `M`，但 `L` 仍持 A 且 eff=8，继续运行）；
- t=12 再释放 A，唤醒 `H` 并立即把处理器交给它；`L` 的有效优先级 **8→1**；
- 同时覆盖了“任务结束仍持锁 → LIFO 自动释放”路径（由测试
  `TestExitAutoReleaseLIFO` 验证，自动释放顺序为 B 后 A）。

### 4.4 死锁反例：AB-BA 环形等待（`deadlock-abba`）

`P1`(3) 先持 A，`P2`(6) 到达后抢占并持 B，随后互要对方的锁：

```
t=4  block     P2  lock=A owner=P1     # P2 持 B 等 A；P1 被提升 3->6
t=6  block     P1  lock=B owner=P2     # P1 持 A 等 B
t=6  deadlock  cycle=P1->P2->P1        # 等待图成环，检测并报告
```

报告中 `deadlocked=true`、`completed=false`，两个任务都停留在 `blocked`
（P1 持 A、P2 持 B）。这表明**优先级继承不能解除环形等待**。

### 4.5 FIFO 与优先级等待队列

`queuePolicy` 支持 `fifo`（默认，按阻塞先后唤醒，保留直接交接）与 `priority`
（按有效优先级唤醒）。`TestFIFOvsPriorityQueue` 用同一负载断言两种策略下首个被
唤醒者分别为先到的低优先级等待者与后到的高优先级等待者。

---

## 5. 结构化事件参考

时间线 `events[]` 中每条记录都带 `tick` 与单调递增的 `seq`。事件类型：

| 事件 | 含义 |
| ---- | ---- |
| `arrive` | 任务到达，进入 ready |
| `dispatch` | 任务被调度上处理器 |
| `preempt` | 运行中任务被抢占（`by` 为抢占者） |
| `cpuTick` | 消耗一个 CPU tick（`remaining` 为该指令剩余 tick） |
| `block` | 任务抢锁失败而阻塞（`owner`/`waiters`） |
| `wakeup` | 阻塞任务在锁释放时获得锁并被唤醒 |
| `lockAcquired` / `lockReleased` | 持锁/放锁（退出自动释放带 `reason`） |
| `donorJoin` / `donorLeave` | 捐赠边建立/消失 |
| `priorityBoost` / `priorityReset` | 有效优先级升高/降低（`oldPrio`→`newPrio`） |
| `idle` | 处理器空闲，时钟快进 `delta` |
| `finish` | 任务完成 |
| `deadlock` | 检出等待环（`cycle` 首尾重复） |
| `fatal` | 非法程序导致中止（如释放未持有的锁、重入自锁、超 maxTicks） |

报告 `Report` 还包含每个任务的 `finishTick / cpuTicks / blockedTicks /
readyTicks / heldLocks / effectivePriority / priorityBoosts` 以及每把锁的最终
持有者与等待队列，便于断言与统计。

---

## 6. 自动化测试与真实运行记录

```bash
go test -count=1 ./...        # 全部包
go test -race  -count=1 ./... # 竞态检测
go test -cover ./scheduler/ ./scenario/ ./httpapi/
gofmt -l . && go vet ./...
```

最终一次真实运行（2026-09-23，`go1.22.2 linux/amd64`）的完整输出保存在
[`examples/output/RUNLOG.txt`](examples/output/RUNLOG.txt)，要点：

- `go build ./...`、`go vet ./...`、`gofmt -l .`：全部通过/无输出；
- `go test -count=1 ./...`：`scheduler`、`scenario`、`httpapi` 三个包 **全部 ok**，
  `-race` 下同样全部通过；
- 语句覆盖率：`scheduler` **90.8%**、`scenario` 84.4%、`httpapi` 72.4%。

测试覆盖：基础抢占、单次/多捐赠者继承与释放回落、无继承对照、三级传递链、
AB-BA 死锁检测与去重、自死锁与非法 unlock、退出 LIFO 自动释放、FIFO/优先级
队列、空闲快进、`maxTicks` 防护、配置校验、事件 Sink 实时推送、**可替换时钟**
（`CountingClock` 断言推进量）、**可替换执行器**（脚本化执行器重放）、以及全部
HTTP 端点（含 SSE、400/405/422 错误路径）。

**未通过项（如实记录）**：在最终代码状态下，`go test ./...` 与 `-race` 均无失败
用例，构建/静态检查也全部通过。开发过程中出现过两处**场景脚本设计**问题并已修正
（它们一度让内置场景没有产生预期现象，而非内核缺陷）：初版“三级继承”脚本意外
构成 AB-BA 环、初版“死锁”脚本因高优先级任务过早连取两锁而未成环；两者通过调整
到达时刻/指令时序修正为现在的非环传递链与经典 AB-BA。此外，开发中还修复了三个
内核问题并由回归测试锁定：唤醒交接时 lock 指令被重复推进（执行器 PC 多走一步）、
`pick()` 漏算正在运行的任务导致 CPU 指令后误抢占、以及零耗时锁操作未作为调度点
导致同优先级任务无法交错。

---

## 7. 设计取舍与非目标

- 这是**离散事件/ tick 级教学模拟器**，不是对接真实 OS 线程的运行时；目标是把
  抢占、继承、传递、回落、死锁的因果关系做成可测试、可观测的确定性模型。
- PIP 而非 PCP（优先级天花板协议）：不对锁预先设天花板，因此保留了死锁可能，
  正好用于死锁反例与检测。
- 不提供避免/恢复死锁的策略（如超时、银行家算法）；检测到环即终止并报告。
- 单处理器；不模拟缓存、中断、多处理器队列迁移等。
- **不做前端**：所有交互通过 CLI、HTTP JSON / SSE 与文件完成。
