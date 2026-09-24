# 离线多机器人任务分配服务（Go / net/http）

一个**纯后端**的离线多机器人任务分配（multi-robot task allocation）服务。给定若干机器人的
位置、电量、载荷能力，若干带时间窗的配送任务、一个取货点（depot）和若干充电点，
服务输出一份可行且**最小化总完工时间（makespan）**的计划，或明确报告无解。

* 仅使用 Go **标准库**（`net/http`、`encoding/json`），**零第三方依赖**。
* 全部时间、距离、电量、载荷均为**整数**（`int64`）。
* 距离采用**曼哈顿距离**，旅行耗时在数值上等于距离；旅行耗能 = 距离 × `energy_per_dist`。
* 求解用深度优先分支定界（分支定界 + Pareto 标签路径搜索 + 贪心初始上界 + 同构机器人剪枝）。
* 对每个可行结果，服务端会再用一份**独立的计划重放校验器**自检后才返回。

---

## 1. 问题模型

* 平面为整数坐标点。两点曼哈顿距离：`|x1-x2| + |y1-y2|`。
* **任务（task）**：货物在 `depot`，机器人需要先到 depot 取货（载荷 `load`），
  再把货物送到 `location`；卸货服务必须在整数时间窗 `[ready, due]` 内**开始**，
  卸货耗时 `service_time`。早到则在任务点等待。
* **机器人（robot）**：
  * 从 `start`、时刻 0、电量 `start_battery` 出发；
  * 电池容量 `battery_capacity`；移动每格耗电 `energy_per_dist`；
  * 在充电点可充电，充入 1 单位电量耗时 `charge_rate`，可充至满电；
  * 同时承载货物总载荷不得超过 `capacity`。
* 一个机器人对一个任务的标准动作序列为：
  `[途中充电点充满]* → depot 取货 → [途中充电点充满]* → 任务点送达`。
  即：取货前、送货途中都允许绕行充电；送达后机器人位于任务点，继续服务下一任务。
* **目标**：最小化 makespan = 最后一台机器人完成最后一个服务动作的时刻。

求解器枚举「任务 → 机器人」的全部分配、每台机器人的全部服务顺序，以及各段路程中
所有 Pareto 非劣的充电路径，因此在搜索预算内是**完备**的：能找到全局最优，
或在预算耗尽前证明无解。

> 规模说明：这是离线**小实例**求解器（输入上限 64 机器人 / 64 任务，默认搜索节点上限
> 200000）。组合问题本身是 NP-hard 的，超大实例会触发节点上限并诚实返回
> `optimal=false`，而不会谎称已证明最优或无解。

---

## 2. 目录结构

```
.
├── go.mod                          # 模块定义；零第三方依赖（无 go.sum）
├── cmd/server/main.go              # HTTP 服务入口
├── examples/                       # 可直接 curl 的请求样例
│   ├── must_charge.json            # 可行：不充电就到不了，必须先充
│   ├── infeasible_window.json      # 无解：时间窗太紧
│   └── two_robots.json             # 两台机器人并行
└── internal/
    ├── model/                      # 领域模型与距离
    ├── route/                      # Pareto 标签路径搜索（含途中多次充电）
    ├── solver/                     # 分支定界求解器
    ├── validate/                   # 独立计划重放校验器
    ├── bruteforce/                 # 独立朴素穷举器（仅用于验收交叉核对）
    └── api/                        # net/http 接口与请求校验
```

`route`、`solver`、`validate`、`bruteforce` 之间：`bruteforce` **刻意不与**
`solver`/`route` 共享任何搜索代码，以便作为独立的正确性参照。

---

## 3. 依赖与启动

### 依赖

* Go ≥ 1.23（开发实测版本 `go1.23.4 linux/amd64`）。
* 无任何第三方包。`go.mod` 即完整的依赖锁定；因为没有外部模块，仓库中不需要 `go.sum`。

### 构建与运行

```bash
# 直接运行
go run ./cmd/server

# 或构建后运行
go build -o robot-task-server ./cmd/server
ADDR=:8080 ./robot-task-server
```

监听地址由环境变量 `ADDR` 控制，默认 `:8080`。

### 接口

| 方法 | 路径        | 说明                                   |
|------|-------------|----------------------------------------|
| GET  | `/healthz`  | 健康检查，返回 `{"status":"ok"}`        |
| POST | `/api/plan` | 提交问题，返回最优可行计划或无解结论     |

`POST /api/plan` 请求体（JSON）：

| 字段 | 类型 | 说明 |
|------|------|------|
| `robots[].id` | string | 机器人 ID（唯一、非空） |
| `robots[].start` | `{x,y}` | 初始坐标 |
| `robots[].start_battery` | int | 初始电量，∈ [0, battery_capacity] |
| `robots[].battery_capacity` | int | 电池容量（>0） |
| `robots[].capacity` | int | 最大同时载荷（>0） |
| `robots[].energy_per_dist` | int | 每格耗电（≥0） |
| `robots[].charge_rate` | int | 充 1 单位电量的时间（≥0） |
| `depot` | `{x,y}` | 取货点 |
| `chargers[]` | `{id,location}` | 充电点（有任务时至少一个） |
| `tasks[].id` | string | 任务 ID（唯一、非空） |
| `tasks[].location` | `{x,y}` | 送货位置 |
| `tasks[].load` | int | 货物载荷（≥0） |
| `tasks[].ready`,`tasks[].due` | int | 时间窗（0 ≤ ready ≤ due） |
| `tasks[].service_time` | int | 卸货耗时（≥0），可省略为 0 |
| `node_limit` | int | 可选，搜索节点上限；默认 200000 |

响应（可行时）：`feasible=true`、`optimal`、`makespan`、`plans[]`，并附带
`validation.valid=true` 的独立校验结果。无解时：`feasible=false`，
`optimal=true` 表示已穷举证明无解；若撞上节点上限则 `optimal=false` 并在 `reason` 说明。

计划中每个动作 `type` 取值：

* `travel`：从 `from` 行驶到 `to`；
* `charge`：行驶到某充电点并充满（`service_time` 为充电耗时）；
* `pickup`：在 depot 的零长度取货事件；
* `service`：在任务点的零长度服务事件；若早到，等待体现在
  `end_time - arrival_time - service_time`。

每个动作都带有 `battery_before`/`battery_after`，可逐段核对电量始终非负。

---

## 4. 请求样例

启动服务后：

```bash
# 1) 必须先充电才可行（makespan = 30）
curl -s -X POST http://127.0.0.1:8080/api/plan \
  -H 'Content-Type: application/json' \
  --data @examples/must_charge.json

# 2) 时间窗太紧，已证无解（feasible=false, optimal=true）
curl -s -X POST http://127.0.0.1:8080/api/plan \
  -H 'Content-Type: application/json' \
  --data @examples/infeasible_window.json

# 3) 两台机器人并行（makespan = 11）
curl -s -X POST http://127.0.0.1:8080/api/plan \
  -H 'Content-Type: application/json' \
  --data @examples/two_robots.json
```

### 样例 1 的计划（`must_charge.json`）

机器人初始只有 10 格电，depot 到任务点 15 格，直达不可能；必须先开到
5 格外的充电点充满，再去送货。关键动作（`battery` 为每段前后电量）：

```
pickup t1   t=0          bat 10 -> 10
charge c1   t=0 -> 20    bat 10 -> 20   (行驶5格到 c1，再花15时间充满)
travel      t=20 -> 30   bat 20 -> 10   (行驶10格到任务点)
service t1  t=30         bat 10 -> 10
makespan = 30
```

也可以用内联 JSON 快速试验：

```bash
curl -s -X POST http://127.0.0.1:8080/api/plan \
  -H 'Content-Type: application/json' -d '{
    "robots":[{"id":"r1","start":{"x":0,"y":0},"start_battery":20,
               "battery_capacity":20,"capacity":10,
               "energy_per_dist":1,"charge_rate":1}],
    "depot":{"x":0,"y":0},
    "chargers":[{"id":"c1","location":{"x":0,"y":5}}],
    "tasks":[{"id":"t1","location":{"x":20,"y":0},"load":1,
              "ready":0,"due":15,"service_time":0}]
  }'
# => {"feasible":false,"optimal":true,...}  时间窗 due=15，最近也要 20
```

---

## 5. 自动化测试与验收方式

```bash
go test ./...                 # 全部测试
go test -race ./...           # 带竞态检测
go test -coverprofile=c.out ./... && go tool cover -func=c.out   # 覆盖率
```

测试覆盖以下验收点：

1. **对小实例穷举核对总完工时间**：`TestBruteForceCrossCheck` 生成数百个随机小实例
   （1–3 台机器人、3–5 个任务，含普通电量与**低电量强制充电**两组，以及紧迫时间窗），
   用**独立穷举器** `bruteforce.MinMakespan` 与求解器逐一比对：
   可行性结论必须一致；都可行时最优 makespan 必须相等。
2. **必须先充电**：`TestMustChargeBeforeTask`、`TestChargeWithServiceTime` 手算核对
   含绕行充电的时刻与电量；`TestTwoChargersMultiHop` 验证途中在两个充电点依次充满。
3. **紧迫时间窗**：`TestTightWindowInfeasible`（赶不上 → 已证无解）、
   `TestTightWindowFeasibleWithExactArrival`（恰好 `arrival==due` 压线可行）、
   `TestWaitingAtTask`（早到等待）。
4. **每段电量非负**：所有可行结果都过 `validate.Check`，逐段重放行驶/充电，
   要求行驶后电量 ≥ 0、充电不超过容量、充电耗时与充入电量一致。
5. **任务恰好分配一次**：校验器强制每个任务恰好被取货一次、服务一次且由同一机器人完成。
6. 其余：容量不足无解、两机并行更优、节点上限时诚实返回 `optimal=false`、
   空任务边界，以及 HTTP 层的坏 JSON、未知字段、非法输入、405 等。

---

## 6. 实测结果

以下为本次交付时在本机（`go1.23.4 linux/amd64`）**实际运行**的记录。

### `go test -race -count=1 ./...`

```
ok  github.com/example/robot-task/internal/api       (coverage 83.1%)
ok  github.com/example/robot-task/internal/route     (coverage 95.2%)
ok  github.com/example/robot-task/internal/solver    (coverage 95.4%)
ok  github.com/example/robot-task/internal/validate  (coverage 76.7%)
?   cmd/server / model / bruteforce                 [no test files]
全语句总覆盖率: 87.7%
```

### 交叉核对日志（摘录）

```
交叉核对完成: 可行 183(其中含充电动作 42), 无解 112, makespan 全部一致
```

（即 295 个随机小实例中，求解器与独立穷举器的可行性结论 100% 一致，
183 个可行实例的最优 makespan 全部相等，其中 42 个计划包含途中充电。）

### 实跑 HTTP 三个样例

```
must_charge         feasible=true  optimal=true  makespan=30
infeasible_window   feasible=false optimal=true  (时间窗无解)
two_robots          feasible=true  optimal=true  makespan=11
```

---

## 7. 已明确的简化与未完成项

* **离线、静态、确定性**：一次性给出整批任务的计划，不支持运行中动态加任务或抢占。
* **取货点模型**：所有货物共享单个 depot；每次取一件、送一件（机器人容量用于约束
  未来扩展的同时取多件；当前每趟一件，故容量主要拦截“单件超重”）。
* **充电语义**：到充电点后总是充到满电（部分充电在“整数时间 + 满电”Pareto 意义下
  不会更优，故未单列部分充电动作）；充电耗时 = 充入电量 × `charge_rate`。
* **距离/耗时**：固定曼哈顿距离且旅行耗时恒等于距离，未引入速度、坡度或交通耗时。
* **规模**：组合问题 NP-hard，求解器面向小实例；超大实例受 `node_limit` 保护并如实
  报告未证明最优，未实现大规模启发式（如 ALNS / 列生成）或并发求解。
* **无界面 / 无鉴权 / 无持久化**：按需求只做纯后端 HTTP；进程内计算，不存计划、
  不带认证与限流，生产暴露前需自行加网关与超时控制。
