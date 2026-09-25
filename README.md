# 优先级继承模型（Priority Inheritance Model, PIM）

纯后端的**单处理器可抢占任务调度 + 互斥锁模拟器**，用 Go 实现经典的
**优先级继承协议**（Priority Inheritance Protocol, Sha–Rajkumar–Lehoczky,
1990）。调度时钟与执行器均可替换；每次状态变更都产生**结构化事件记录**，
最终输出完整的调度时间线 JSON。

本仓库不包含任何前端。

---

## 1. 它能演示什么

| 验收点 | 内置场景 | 关键证据 |
|---|---|---|
| 经典无界**优先级反转** | `classic-inversion-no-pi` | 关闭继承时 Medium 在 High 阻塞期间抢占 Low，`inversionTicks=4`，High 在 t=13 才完成 |
| 继承修复反转 | `classic-inversion-pi` | Low 继承 High 优先级 3，`inversionTicks=0`，High 在 t=9 完成 |
| **三级嵌套锁的传递继承** | `three-level-inheritance` | 链 `Urgent(4) →R2→ High(3) →R1→ Low(1)`，Low 有效优先级 `1→3→4`，释放后 `4→1` |
| 释放后**重算有效优先级** | `inherit-restore` / 所有多锁场景 | `priority_change` 事件携带 reason=`grant` 的回落 |
| **多锁释放顺序** | `multi-lock-r1-first` / `multi-lock-r2-first` | `lock_grant` 顺序不同，等待者阻塞 tick 数相反 |
| **死锁反例 + 等待环检测** | `deadlock-ab-ba` | 即便开启 PI 仍出现 AB/BA 环；在 t=1 检出 `T1→T2→T1` 并停机 |
| 可替换时钟 | `internal/clock` | `VirtualClock`（默认，离散确定）与 `WallClock` |
| 可替换执行器 | `internal/executor` | `RecordingExecutor` / `PacedExecutor`，实现 `scheduler.Executor` 即可注入 |
| 结构化事件 | `internal/event` | 14 种事件类型，`seq` 稠密、时间单调，JSON 可直接消费 |

> 优先级约定：**数值越大，优先级越高**；同优先级按规格声明顺序（早者优先），
> 保证结果完全确定。

---

## 2. 目录结构

```
.
├── go.mod
├── cmd/
│   ├── pim/            # CLI：读内置场景或 JSON 文件，输出完整结果 JSON
│   └── server/         # 本地 HTTP 服务
├── internal/
│   ├── clock/          # 可替换时钟（Virtual / Wall）
│   ├── event/          # 事件类型、Sink（Memory/Channel/Multi）
│   ├── program/        # 任务程序：动作序列（cpu / acquire / release）
│   ├── scheduler/      # 调度引擎：抢占、继承、释放重算、死锁检测
│   ├── executor/       # 可替换执行器（录制 / 实时节拍）
│   ├── scenario/       # 七个内置教学场景
│   └── httpapi/        # net/http 接口（标准库，无第三方依赖）
├── examples/           # 可直接 POST 的请求样例 JSON
├── docs/
│   ├── API.md          # HTTP 接口与字段说明
│   ├── TIMELINES.md    # 所有场景的人类可读时间线（由真实运行生成）
│   └── output/         # 每次真实运行的完整结果 JSON + 时间线 md
└── README.md
```

无任何第三方 Go 依赖，`go.mod` 只声明模块与 Go 版本。

---

## 3. 快速开始

需要 Go 1.22+（实际开发与验证使用 go1.22.2）。

### 3.1 运行自动化测试

```bash
go vet ./...
go test ./... -count=1
```

### 3.2 CLI：跑内置场景

```bash
go run ./cmd/pim -scenario classic-inversion-no-pi        # 反转反例
go run ./cmd/pim -scenario classic-inversion-pi           # 继承修复
go run ./cmd/pim -scenario three-level-inheritance        # 三级传递继承
go run ./cmd/pim -scenario deadlock-ab-ba                 # 死锁检测
```

### 3.3 CLI：跑请求文件

```bash
go run ./cmd/pim -file examples/three-level-inheritance.json
# 或从标准输入
cat examples/deadlock-ab-ba.json | go run ./cmd/pim -file -
```

`-fail-deadlock` 可让死锁运行以退出码 2 结束（便于脚本断言）。

### 3.4 HTTP 服务

```bash
go run ./cmd/server -addr 127.0.0.1:8080
# 另一个终端：
curl -s http://127.0.0.1:8080/api/scenarios
curl -s http://127.0.0.1:8080/api/scenarios/deadlock-ab-ba
curl -s -X POST http://127.0.0.1:8080/api/simulate \
     -H 'Content-Type: application/json' \
     --data @examples/classic-inversion-no-pi.json
```

接口与全部字段见 [`docs/API.md`](docs/API.md)。

---

## 4. 请求模型（Spec）

```json
{
  "name": "任意名称",
  "resources": ["R1", "R2"],
  "options": {
    "priorityInheritance": true,
    "deadlockDetection": true
  },
  "tasks": [
    {
      "id": "Low",
      "arrival": 0,
      "priority": 1,
      "program": {
        "actions": [
          {"cpu": 1},
          {"acquire": "R1"},
          {"cpu": 6},
          {"release": "R1"}
        ]
      }
    }
  ]
}
```

- 动作三选一（每次必须且只能设置一个字段）：
  - `{"cpu": n}`：占用处理器 n 个 tick；
  - `{"acquire": "R"}`：阻塞式 P 操作；
  - `{"release": "R"}`：V 操作，锁被直接移交给最高优先级等待者。
- 锁为**非递归互斥量**；重复获取自己持有的锁是 `program_error`。
- 任务可同时持有多把锁（嵌套）；任务结束时仍持有的锁会被强制移交等待者。
- `deadlockDetection` 默认 `true`：检出等待环时发出 `deadlock` 事件并终止；
  设为 `false` 时不会挂起，而以 `Result.error` 终止（封闭模型中环必无进展）。

---

## 5. 事件与时间线

每次运行返回 `scheduler.Result`，其中 `events` 为有序数组，每个事件：

```json
{
  "seq": 0,                 // 稠密、从 0 开始的分配序号
  "time": 0,                // 逻辑 tick
  "kind": "task_dispatch",  // 14 种事件之一
  "task": "Low",            // 主任务（可省略）
  "resource": "R",          // 锁（可省略）
  "detail": { }             // 类型相关的结构化负载
}
```

事件种类：`task_arrive / task_dispatch / task_wakeup / task_block /
task_preempted / task_exit / lock_acquire / lock_release / lock_grant /
priority_change / tick / idle_jump / deadlock / program_error`。

核心事件语义：

- `priority_change.detail` 含 `base / old / new / donors / reason /
  inheritanceOn`；`donors` 是把优先级沿阻塞链传给该任务的所有等待者；
- `lock_grant.detail.from` 标明释放者，`waitersRemaining` 为剩余等待数；
- `deadlock.detail.cycle` 为闭合环（首尾同名），`edges` 为逐条等待边。

完整真实输出见 [`docs/TIMELINES.md`](docs/TIMELINES.md) 与
[`docs/output/`](docs/output/)。

---

## 6. 引擎如何工作（简述）

1. 每个调度边界先**接纳**到达任务，再按**有效优先级**抢占式挑选唯一运行者；
2. 运行者连续排空零耗时的加/解锁动作；遇到 `cpu` 动作后每次只推进 1 tick，
   以便新到达的高优先级任务立即抢占；
3. 阻塞发生时建立边 `等待者 → 锁持有者`，随后从全部阻塞链**重新计算**
   每个任务的有效优先级（不动点迭代：`eff(T)=max(base(T), max eff(D))`），
   因而传递继承自动支持任意深度嵌套；
4. 释放锁时把锁交给当前最高有效优先级等待者，再重算全部有效优先级（多余
   继承即时撤销，不需要记账式的“恢复到某层”）；
5. 每次阻塞后做 wait-for 图三色 DFS，检出环即报告死锁。

无处理器空闲时，时间跳到下一个到达点（`idle_jump` 事件）。

---

## 7. 可替换接缝

```go
res, err := scheduler.RunWith(spec, scheduler.Config{
    Clock:    clock.NewVirtualClock(0), // 也可用自定义 clock.Clock
    Sink:     mySink,                   // 实现 event.Sink 即可流式订阅
    Executor: executor.NewRecording(),  // 每个处理器 tick 回调一次
})
```

- **时钟**：实现 `clock.Clock`（`Now / Advance / WallTime`）。
- **执行器**：实现 `scheduler.Executor`（`Tick(TaskExecution)`），
  可用作真实节拍（`executor.PacedExecutor`）或旁路记录。
- **事件出口**：实现 `event.Sink`（`Emit(Event)`），另有
  `MemorySink` / `ChannelSink` / `MultiSink`。
- **程序**：默认 `ScriptProgram`；实现 `program.Program` 可接入其它行为源。

---

## 8. 验证与如实记录

实际运行命令、输出与任何未通过项都记录在
[`docs/VALIDATION.md`](docs/VALIDATION.md)。该文档由开发过程中真实执行生成，
包括 `go test -v` 全部 22 个测试函数、7 个场景 CLI 运行与 HTTP 端到端 curl 验证。
