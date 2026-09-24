# 离线多机器人任务分配服务（Go / net/http）

纯后端服务：给定若干机器人的**初始位置、电量、电池容量、充电速率、载荷能力**，若干带
**载荷与硬时间窗 `[ready, due]` 的任务**，以及若干**充电点**，求出一份使**总完工时间
（makespan，最后一个任务完成的时刻）最小**的可行计划；若不存在可行计划，明确返回无解及
原因。

只使用 Go 标准库（`net/http`、`encoding/json`），无任何第三方依赖。

## 1. 模型约定（所有量均为整数）

| 概念 | 定义 |
|---|---|
| 位置 | 二维整数网格点 `(x, y)` |
| 行驶时间 | 两点的**曼哈顿距离**（单位速度），`time == distance` |
| 行驶耗能 | `energyPerDist × distance`，默认为 1 |
| 任务时间窗 | 服务可在整数时刻 `t ∈ [ready, due]` 开始；早到则等待；服务持续 `service` 个时间单位 |
| 载荷 | 机器人 `payloadCapacity < task.payload` 时不能执行该任务（单任务独立执行） |
| 充电 | 到达充电点后停留 `⌈(容量 − 当前电量)/chargeRate⌉` 个时间单位，**充满**后离开 |
| 目标 | 最小化 makespan；每个任务必须被**恰好一个机器人服务一次** |
| 约束 | 任一行驶段开始前电量必须 ≥ 该段耗能（即全程每段电量非负） |

**关于充电绕路**：求解器在两个相邻任务之间只考虑“至多绕一个充电点”。这不是近似——因为
充电一次即充满，两个任务之间绕两个充电点能做到的，绕最近/最有利的一个同样能做到；穷举中
加入第二个充电点是冗余的。

## 2. 求解算法

`solver/solver.go` 是**精确穷举 + 分支限界**，不是启发式：

- 每个搜索节点枚举 `(下一个任务, 执行机器人, 直达或先绕某个充电点)` 的全部可行组合；
- 递归到所有任务分配完即得到一份完整计划；
- 以当前最优 makespan 为上界剪枝（任何机器人已空出时间 ≥ 上界即剪掉），按截止时间紧迫的
  任务优先分支以尽早得到紧上界；
- 对能力与当前状态完全相同的机器人做对称剪枝。

因此返回的可行计划保证是最优的，返回无解则证明所有任务排列、机器人分配与充电绕路组合都
不可行。为保证穷举可行，实例规模上限：**机器人 8、任务 10、充电点 6**（超出返回 400）。

测试包内另有一份**完全独立编写的参考穷举器**（按 `(任务子集, 末位置)` 维护
`(时间, 电量)` Pareto 前沿，再组合各机器人路线求最优 makespan），对定向场景和 60 个随机
小实例逐一交叉核对两个实现的最优值与可行性，并用 `verifyPlan` 逐条校验：

- 每个行驶段耗电前电量足够 → **每段电量非负**；
- 每个任务服务开始时刻 ∈ `[ready, due]`，载荷满足；
- **每个任务恰好出现一次**；
- 充电时长、充满、到达/结束时刻、位置衔接全部自洽；
- 报告的 makespan 等于各机器人完工时间的最大值。

## 3. 依赖

- Go 1.23+（开发与实测版本：**go1.23.4 linux/amd64**）
- 第三方依赖：**无**。`go.mod` 锁定工具链版本；因为只引用标准库，所以没有也不需要
  `go.sum`（`go mod tidy` 实测不生成）。

## 4. 启动命令

```bash
# 方式一：HTTP 服务（默认 :8080）
go run .
# 或指定端口
go run . -addr :9090

# 方式二：离线一次性求解一个 JSON 文件，打印结果后退出（无需起服务）
go run . -file examples/must_charge.json

# 编译为二进制
go build -o robotdispatch . && ./robotdispatch -addr :8080
```

## 5. HTTP 接口

| 方法与路径 | 说明 |
|---|---|
| `GET /healthz` | 健康检查 |
| `GET /` | 服务与接口说明 |
| `POST /api/v1/plans` | 求解一个分配实例 |

请求体（`application/json`，上限 1 MiB）：

```json
{
  "energyPerDist": 1,
  "robots": [
    {"id": "r1", "start": {"x": 0, "y": 0},
     "battery": 6, "batteryCapacity": 20,
     "chargeRate": 2, "payloadCapacity": 10}
  ],
  "tasks": [
    {"id": "j1", "loc": {"x": 10, "y": 0}, "payload": 1,
     "ready": 0, "due": 100, "service": 1}
  ],
  "chargers": [
    {"id": "c1", "loc": {"x": 5, "y": 0}}
  ]
}
```

字段约束：`batteryCapacity > 0`、`0 ≤ battery ≤ batteryCapacity`、`chargeRate > 0`、
`payloadCapacity ≥ 0`、`0 ≤ ready ≤ due`、`service ≥ 0`、各类 id 非空且不重复。
`energyPerDist` 省略或为 0 时按 1 处理。

成功（可行）响应 `200`：

```json
{
  "feasible": true,
  "makespan": 21,
  "plans": [
    {
      "robotID": "r1",
      "start": {"x": 0, "y": 0},
      "steps": [
        {"type": "charge", "chargerID": "c1",
         "from": {"x":0,"y":0}, "to": {"x":5,"y":0},
         "moveDist": 5, "moveTime": 5, "moveCost": 5,
         "arriveTime": 5, "startTime": 5, "chargeTime": 10, "endTime": 15,
         "batteryBefore": 1, "batteryAfter": 20},
        {"type": "task", "taskID": "j1",
         "from": {"x":5,"y":0}, "to": {"x":10,"y":0},
         "moveDist": 5, "moveTime": 5, "moveCost": 5,
         "arriveTime": 20, "startTime": 20, "endTime": 21,
         "batteryBefore": 15, "batteryAfter": 15}
      ],
      "finishAt": 21
    }
  ]
}
```

无可行解时同样返回 `200`（业务结果而非协议错误）：

```json
{"feasible": false, "reason": "infeasible: no assignment satisfies ..."}
```

请求格式/校验错误返回 `400`，方法错误返回 `405`（带 `Allow` 头）。

### curl 样例

```bash
# 必须先充电才能到任务（直达电量不足）
curl -s localhost:8080/api/v1/plans -H 'Content-Type: application/json' \
  -d @examples/must_charge.json

# 无充电点 → 明确无解
curl -s localhost:8080/api/v1/plans -H 'Content-Type: application/json' \
  -d @examples/no_charger_infeasible.json

# 两个紧迫时间窗（due 恰好等于直达到达时刻），必须分两个机器人
curl -s localhost:8080/api/v1/plans -H 'Content-Type: application/json' \
  -d @examples/tight_windows.json

# 多机器人 + 混合能力 + 充电点
curl -s localhost:8080/api/v1/plans -H 'Content-Type: application/json' \
  -d @examples/multi_robot.json
```

## 6. 示例文件

| 文件 | 场景 | 实测结果 |
|---|---|---|
| `examples/must_charge.json` | 初始电量 6、任务距离 10，必须先在 c1 充电 | 可行，makespan **21**（5 行驶 + 10 充电 + 5 行驶 + 1 服务），到达充电桩时电量恰为 1 |
| `examples/no_charger_infeasible.json` | 同上但无充电点 | **无解**，返回 infeasible 原因 |
| `examples/tight_windows.json` | 两个任务的 due 分别等于到各自位置的直达时刻 | 可行，makespan **8**，必须两个机器人各取一个 |
| `examples/multi_robot.json` | 载荷能力不同、一个机器人初始不满电、3 任务 1 充电桩 | 可行，makespan **19**，重载荷任务 j2 只能给 r2 |

## 7. 自动化测试

```bash
go test ./...                 # 全部单元/HTTP 测试
go test -race ./...           # 竞态检测
go vet ./...                  # 静态检查
go test -tags stress ./solver/ -run TestStressLargest -v   # 上限规模(10任务)对拍，约 4 秒
```

测试内容：

- `solver/solver_test.go`
  - `TestMustChargeBeforeFirstTask` / `TestMustChargeNoChargerInfeasible`：必须先充电，以及
    无充电点时无解；
  - `TestTightTimeWindows` / `TestTightWindowInfeasible`：紧迫时间窗可行与不可行；
  - `TestPayloadInfeasible`：载荷不足无解；
  - `TestChargeBetweenTasks`：验证充电步骤**位于两个任务服务之间**；
  - `TestEmptyTasks`：空任务集；
  - `TestRandomInstancesMatchBruteForce`：60 个随机小实例，与独立穷举器对拍可行性与最优
    makespan，并用 `verifyPlan` 校验**每段电量非负、时间窗、每任务恰好一次、衔接自洽**；
  - `stress_test.go`（build tag `stress`）：规模上限 10 任务 / 2 机器人 / 2 充电桩对拍。
- `api/server_test.go`：健康检查、可行/无解求解、坏 JSON、校验错误、空请求体、405。

### 实测结果（2026-09-24，go1.23.4 linux/amd64）

```
$ go test ./... -count=1
?   robotdispatch        [no test files]
?   robotdispatch/model  [no test files]
ok  robotdispatch/api     0.009s
ok  robotdispatch/solver  0.003s

$ go test -race -count=1 ./...
ok  robotdispatch/api     1.038s
ok  robotdispatch/solver  1.033s

$ go test -tags stress ./solver/ -run TestStressLargest -v
feasible=true makespan=20 bruteForce=20 time=3.74s
PASS
```

HTTP 服务已实际用 curl 验证：`/healthz` 返回 200；可行实例返回最优计划；无充电点实例返回
`feasible:false`；坏 JSON 返回 400；GET 提交返回 405。

## 8. 未完成项 / 已知限制（如实说明）

1. **规模上限**：精确穷举的复杂度随任务数阶乘级增长，故硬性限制 8 机器人 / 10 任务 /
   6 充电点；上限规模（10 任务）实测约 3.7 秒，更大实例不适合穷举，需要换用 MIP/CP 或
   分支定价等方法（本项目未实现）。
2. **距离模型单一**：只支持曼哈顿距离 + 单位速度的整数网格，没有欧氏距离、非对称时间矩阵
   或每边自定义耗时/耗能的入口。
3. **充电策略固定为“充满”**，未实现“充到指定电量”的部分充电（部分充电不会优于充满，只是
   在充电速率很快时偶尔更省时；当前模型下充满是最优策略）。
4. **任务间无先后依赖、无任务-任务共载**：每个任务由单个机器人独立执行；未建模多机器人
   协作搬运或取-送配对任务。
5. **不要求任务结束后返回起点/仓库**，makespan 只计到最后一个任务完成；计划中也不包含
   “回家”段。
6. 无鉴权、限流与持久化（题目要求纯后端离线服务，均按不需要处理）；服务关闭为优雅关闭
   （SIGINT/SIGTERM）。
