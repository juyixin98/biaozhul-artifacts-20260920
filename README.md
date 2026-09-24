# 优先级继承模拟（Priority Inheritance Protocol Simulator）

一个**确定性**的离散时间实时调度模拟器，用 Go 标准库 `net/http` 提供纯后端 HTTP 接口。
支持任务的优先级、释放时间、CPU 执行片段与互斥锁（lock/unlock）操作，可开关
**优先级继承协议（PIP）**，并输出每一个时间点的调度决策轨迹，便于对比启用继承前后
高优先级任务的阻塞时长。

- 纯后端，无任何前端/界面
- 仅依赖 Go 标准库（`net/http`、`encoding/json`），**零第三方依赖**
- 确定性：相同输入永远产生字节级一致的事件轨迹（`TestDeterminism` 重复验证）
- 支持：抢占式固定优先级调度、传递式优先级继承、锁释放后优先级正确恢复、
  多等待者按有效优先级授予、空闲时间快进、死锁（等待环）检测

---

## 1. 依赖与启动

| 项 | 要求 |
|---|---|
| Go | 1.21+（开发与实测使用 go1.23.4 linux/amd64） |
| 第三方依赖 | 无（`go.mod` 不 require 任何外部模块，因此无需 `go.sum`） |

```bash
# 构建
go build -o pi-sim .

# 启动（默认监听 :8080）
./pi-sim -addr :8080

# 或直接运行
go run . -addr :8080
```

启动后：

```bash
curl http://127.0.0.1:8080/api/health
# { "status": "ok" }
```

### 运行测试与基准

```bash
go test ./...              # 全部单元测试 + HTTP 接口测试
go test -race ./...        # 带竞态检测
go test -bench . ./sim/    # 性能基准
```

---

## 2. 模型约定

- **时间**：整数“拍（tick）”，区间 `[t, t+1)` 内 CPU 执行一拍。
- **优先级**：`base_priority` 数值越大优先级越高（如 30 > 20 > 10）。
- **任务程序**：一个操作序列，操作分三类：
  - `{"kind":"compute","duration":N}`：占用 CPU N 拍。
  - `{"kind":"lock","resource":"R"}`：申请互斥锁，**零耗时**；锁被占用则阻塞。
  - `{"kind":"unlock","resource":"R"}`：释放锁，**零耗时**。
- **调度规则**：每个时间点先处理“释放事件 + 所有零耗时锁操作”，再从就绪集合里选
  有效优先级最高者执行一拍；同优先级优先保持当前运行者（避免无谓切换），再比释放时间、ID。
- **优先级继承（PIP）**：任务 H 阻塞在锁 R 上时，R 的持有者有效优先级提升到
  `max(自身, H 的有效优先级)`；该提升沿“等待—持有”边**传递**（H→M→L）。
  任务释放相关锁、不再需要替任何等待者继承后，有效优先级恢复为基优先级。
- **授予策略**：释放锁时授予当前有效优先级最高的等待者（PIP 下等待者自身也可能被提升）。
- **死锁**：若就绪集合为空但仍有阻塞任务，判定存在等待环，输出环路径并结束本次模拟
  （死锁是一种合法的模拟结果，HTTP 仍返回 200，见响应中的 `deadlocked`）。

---

## 3. HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET  | `/api/health` | 健康检查 |
| GET  | `/api/presets` | 列出内置场景 |
| GET  | `/api/presets/{name}` | 取内置场景的 workload |
| POST | `/api/presets/{name}/run?inheritance=0\|1` | 直接运行内置场景 |
| POST | `/api/simulate` | 运行单次模拟（请求体含开关 + workload） |
| POST | `/api/compare` | 同一 workload 各跑一次（关/开 PIP）并对比阻塞时长 |

状态码：`200` 成功；`400` JSON 非法/含未知字段；`404` 路径或场景不存在；
`405` 方法不允许；`422` workload 校验失败。请求体上限 1 MiB。

### 3.1 `POST /api/simulate`

请求体：

```json
{
  "inheritance": true,
  "workload": {
    "name": "可选名称",
    "resources": ["R"],
    "tasks": [ /* TaskSpec[] */ ],
    "max_ticks": 100000
  }
}
```

### 3.2 `POST /api/compare`

请求体**只有 workload**（不含 `inheritance`）：

```bash
curl -s -X POST http://127.0.0.1:8080/api/compare \
  -H 'Content-Type: application/json' \
  --data @examples/compare_nested.json
```

响应含 `without_inheritance` 与 `with_inheritance` 两份完整结果，以及每个任务的
阻塞时长对比和汇总 `blocked_delta`（正值表示 PIP 减少了阻塞）。

### 请求样例文件

| 文件 | 用途 |
|---|---|
| `examples/simulate_basic_no_pip.json` | 基础优先级反转，PIP 关闭 |
| `examples/simulate_basic_with_pip.json` | 基础优先级反转，PIP 开启 |
| `examples/compare_nested.json` | 嵌套锁链对比（请求体用于 `/api/compare`） |
| `examples/deadlock.json` | AB/BA 反向加锁死锁 |

```bash
# 单次模拟（开启继承）
curl -s -X POST http://127.0.0.1:8080/api/simulate \
  -H 'Content-Type: application/json' \
  --data @examples/simulate_basic_with_pip.json

# 直接运行内置场景
curl -s -X POST "http://127.0.0.1:8080/api/presets/nested/run?inheritance=1"
```

---

## 4. 响应中的事件轨迹（逐步决策）

`Result.events[]` 按时间顺序记录每个决策，`type` 取值：

| type | 含义 |
|---|---|
| `release` | 任务到达释放时间，进入就绪 |
| `lock-acquired` | 锁空闲，直接获得 |
| `lock-blocked` | 锁被持有而阻塞（事件 `reason` 标明持有者） |
| `priority` | 有效优先级变化（提升 / 恢复；`from_priority`→`to_priority`，含继承链说明） |
| `unlock` | 释放锁 |
| `lock-granted` | 锁被授予某个等待者 |
| `tick` | **本拍调度决策**：选中谁、当时 ready 列表与 blocked 映射、选中原因 |
| `complete` | 任务执行结束 |
| `idle` | 无就绪任务，时间快进到下一个释放点 |
| `deadlock` | 检测到等待环 |

每个 `tick` 事件都带：`selected`、`ready`（按有效优先级排序）、
`blocked`（任务→等待的资源）、`eff_priority`、`remaining` 与人类可读 `reason`，
因此可以完整复盘“这一拍为什么是它在跑”。

---

## 5. 验收场景与实测结果

> 以下数字均为程序实际运行输出（go1.23.4），不是手算预期。复现：
> `curl -X POST .../api/compare` 或 `go test -run TestBasicInversion -v`。

### 5.1 场景一：高/中/低 + 中优先级干扰（内置 `basic`）

- Low(10) t=0 拿到 R，临界区 6 拍；
- High(30) t=3 释放，要拿 R，被 Low 阻塞；
- Medium(20) t=4 释放，纯 CPU 4 拍——无继承时它会插队到 Low 前面。

**关键时间点（开启 PIP）**：t=3 High 阻塞 → Low 立即被提升到 30，
Medium(20) 无法插队；t=6 Low 释放 R 并恢复为 10，High 立即运行。

实测对比（`/api/compare`）：

| 任务 | 阻塞(无PIP) | 阻塞(PIP) | 完成时刻(无PIP) | 完成时刻(PIP) |
|---|---|---|---|---|
| High   | **7** | **3** | 13 | **9** |
| Medium | 0 | 0 | 8 | 13 |
| Low    | 0 | 0 | 16 | 16 |

完成顺序：无 PIP 为 `Medium → High → Low`；开启 PIP 为 `High → Medium → Low`。
High 的阻塞从 7 拍降到 3 拍（被 Medium 插队的 4 拍被消除）。

### 5.2 场景二：嵌套锁链 + 传递继承（内置 `nested`）

- Low(10) 持有 R1；
- Medium(20) 先拿 R2，t=4 再申请 R1 被 Low 阻塞，但**仍持有 R2**；
- High(30) 同一刻 t=4 申请 R2 被 Medium 阻塞；
- 另有 Busy(15) t=3 释放、纯 CPU，充当干扰者。

t=4 settle 后形成等待链 `High ──R2──▶ Medium ──R1──▶ Low`，实测产生的
优先级事件（原始终端输出）：

```
t=4  Low     10->30  | inherited priority 30 via: High --waits R2--> Medium --waits R1--> Low
t=4  Medium  20->30  | inherited priority 30 via: High --waits R2--> Medium
t=8  Low     30->10  | priority restored to base after inherited lock was released
t=11 Medium  30->20  | priority restored to base after inherited lock was released
```

t=4 的逐步决策（Low 被直接提升到 30，于是它——而不是 Busy(15)——继续执行临界区）：

```
t=4 release       task=High
t=4 lock-blocked  task=High    (等 R2)
t=4 lock-blocked  task=Medium  (等 R1)
t=4 priority      task=Low     10->30
t=4 priority      task=Medium  20->30
t=4 tick          task=Low  eff=30  ready=['Low','Busy']  blocked={'High':'R2','Medium':'R1'}
t=5 tick          task=Low  eff=30  ...
```

实测对比：

| 任务 | 阻塞(无PIP) | 阻塞(PIP) | 完成时刻(无PIP) | 完成时刻(PIP) |
|---|---|---|---|---|
| High   | **11** | **7** | 18 | **14** |
| Medium | 8 | 4 | 20 | 16 |
| Busy   | 0 | 0 | 8 | 20 |
| Low    | 0 | 0 | 24 | 24 |

完成顺序：无 PIP 为 `Busy → High → Medium → Low`；开启 PIP 为
`High → Medium → Busy → Low`。High 阻塞减少 4 拍，且 Low 的恢复发生在它释放 R1 之后、
Medium 的恢复发生在它释放 R2 之后（锁释放后正确恢复）。

### 5.3 死锁检测

`examples/deadlock.json`（T1 按 A→B、T2 按 B→A 加锁）实测：

```
deadlocked: true   cycle: ["T1","T2","T1"]   ticks: 3
```

---

## 6. 自动化测试清单

`go test ./...` 全部通过（19 个顶层测试，含若干子测试）：

- `sim` 包：`TestBasicInversion`、`TestNestedChain`（含传递提升、无 PIP 不出现
  priority 事件、compare 数值）、`TestPriorityRestore`（释放后 30→10 恢复）、
  `TestHighestWaiterGrant`（释放时授予最高优先级等待者而非最早排队者）、
  `TestDeterminism`、`TestDeadlock`、`TestIdleAdvance`、`TestFinishesOnZeroOp`、
  `TestValidationErrors/*`（10 个非法输入）、`TestMaxTicks`。
- HTTP 层：健康检查、`/simulate`、`/compare`、presets 系列、400/404/405/422、
  未知字段拒绝。
- 基准：`BenchmarkExecuteNested` ≈ 78 µs/次，`BenchmarkCompareNested` ≈ 155 µs/次
  （随机器波动，仅量级参考）。

---

## 7. 项目结构

```
.
├── go.mod                  # 模块 pi-sim；无 require，故无 go.sum
├── main.go                 # net/http 服务、路由与请求处理
├── main_test.go            # HTTP 接口测试
├── sim/
│   ├── sim.go              # 输入/输出/报告的数据结构
│   ├── validate.go         # workload 静态校验
│   ├── engine.go           # 确定性调度引擎（继承开/关、死锁检测、对比）
│   ├── presets.go          # 内置验收场景 basic / nested
│   ├── engine_test.go      # 引擎测试
│   └── bench_test.go       # 基准测试
├── examples/               # curl 请求样例
└── README.md
```

---

## 8. 如实记录：已完成与未完成 / 边界

**已完成并实测**

- [x] 确定性离散时间调度（释放/抢占/同优先级保持运行者/空闲快进）
- [x] 互斥锁的申请、阻塞、释放、按有效优先级授予多等待者
- [x] 传递式优先级继承（High→Medium→Low），同一时间点批量形成等待图后再传播
- [x] 锁释放后有效优先级恢复基优先级
- [x] 每拍调度决策、优先级变化、阻塞/授予的完整事件轨迹
- [x] 开/关 PIP 两次运行的阻塞时长对比接口
- [x] 死锁（等待环）检测与环路径输出
- [x] 高/中/低 + 中优先级干扰、嵌套锁链两类验收场景及实测数字
- [x] 输入校验、错误状态码、零第三方依赖、自动化测试与基准

**建模上的简化 / 未覆盖项（如实说明）**

- 未实现优先级天花板协议（Priority Ceiling Protocol），只实现了优先级继承。
- 互斥锁为**非递归锁**：同一任务重复 lock 同一资源会被校验拒绝；unlock 顺序必须
  与 lock 配对（程序级静态校验）。
- 不模拟任务挂起/恢复、信号量（多实例资源）、周期任务的自动重复释放；
  每个任务只释放一次。
- 同一时间点多个同优先级任务的首次选择按“释放时间、ID”决出，属确定性 tie-break，
  不是真实 RTOS 的时间片轮转。
- 离散整数时间模型，不涉及执行时间抖动、中断、缓存等真实硬件效应。
- 模拟器本身是纯计算库，HTTP 层未加鉴权/限流/TLS（定位为本地/实验工具）。
