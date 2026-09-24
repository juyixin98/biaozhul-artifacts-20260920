# 带迟滞的离线扩缩容控制器（offline-scaler）

一个**纯后端、离线回放式**的副本扩缩容控制器。给定时间序列负载指标与策略参数，
在离散的决策时刻逐步推进，输出每一拍的动作（扩容 / 缩容 / 保持）以及
**该决策实际使用了哪些样本、为什么这么判**。

- 语言：Go（开发与实测版本 go1.23.4 linux/amd64）
- 依赖：**仅 Go 标准库 `net/http`，零第三方依赖**（`go.mod` 即依赖锁定，无 `go.sum`）
- 无界面、无数据库、无后台 goroutine；引擎是纯函数式的状态机，可任意回放

---

## 1. 目录结构

```
.
├── go.mod                          # 模块声明（无 require 块 = 依赖完全锁定在标准库）
├── README.md
├── cmd/
│   ├── scaler/main.go              # HTTP 服务
│   └── scenarios/main.go           # 三场景离线回放 / 样例请求生成
├── internal/
│   ├── scaler/
│   │   ├── scaler.go               # 决策引擎（迟滞/限速/冷却/冷启动/窗口/缺失处理）
│   │   ├── replay.go               # 离线回放编排
│   │   ├── scenarios.go            # 三个验收场景（尖峰/增长/振荡）
│   │   ├── reasons.go              # 中文决策理由
│   │   ├── scaler_test.go          # 引擎单元测试（13 个）
│   │   └── scenarios_test.go       # 场景验收测试 + 输入校验测试（4 个）
│   └── api/
│       ├── server.go               # net/http 路由与编解码
│       └── server_test.go          # HTTP 接口测试（7 个）
└── examples/
    ├── spike-request.json          # 场景一请求样例
    ├── ramp-request.json           # 场景二请求样例
    ├── oscillation-request.json    # 场景三请求样例（含显式缺失样本）
    └── manual-request.json         # 手工小样例，覆盖完整保护链路
```

---

## 2. 启动与运行

前置条件：Go 1.23+（仅用标准库，无需联网下载任何模块）。

```bash
# 编译检查
go build ./...

# 启动 HTTP 服务（默认 :8080，可用 -addr 改）
go run ./cmd/scaler -addr :8080

# 直接离线回放三个验收场景，打印中文决策表（无需启动服务）
go run ./cmd/scenarios                 # 三场景全部
go run ./cmd/scenarios -only spike     # 只看某一个：spike | ramp | oscillation
go run ./cmd/scenarios -json           # 输出完整 JSON（含 usedSamples 与每条理由）

# 把三场景的请求体重置为 examples/*.json
go run ./cmd/scenarios -write-dir .

# 测试
go test ./...              # 全部 24 个测试
go test -cover ./...       # 覆盖率（核心包见第 6 节实测）
go test -race ./...        # 竞态检测
go vet ./...
```

依赖锁定说明：项目没有任何 `require` 第三方模块，构建不访问模块代理；
Go 版本由 `go.mod` 的 `go 1.23` 指令约束（可用 `GOTOOLCHAIN=local` 禁止自动下载工具链）。

---

## 3. HTTP 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/api/v1/replay` | 提交策略 + 样本 + 决策时刻，返回逐拍决策与理由 |
| `GET`  | `/healthz` | 健康检查 |
| `GET`  | `/` | 接口清单 |

请求体（JSON）：

```json
{
  "config": {
    "minReplicas": 2,
    "maxReplicas": 10,
    "loadPerReplica": 100,
    "upWindowSeconds": 60,
    "downWindowSeconds": 300,
    "upThresholdPercent": 10,
    "downThresholdPercent": 20,
    "maxUpStep": 2,
    "maxDownStep": 1,
    "upCooldownSeconds": 60,
    "downCooldownSeconds": 120,
    "scaleUpStabilizationSeconds": 180,
    "coldStartDelaySeconds": 120,
    "freshnessSeconds": 45,
    "maxSampleGapSeconds": 45
  },
  "initialReplicas": 0,
  "decisionIntervalSeconds": 60,
  "decisionTimes": [0, 60, 120],
  "samples": [
    {"timeSeconds": 0, "totalLoad": 150},
    {"timeSeconds": 30, "missing": true}
  ]
}
```

字段说明：

- `config` 各字段**省略（零值）时采用默认值**，因此完整策略建议显式给出。
  - 期望副本 = `ceil(窗口峰值总负载 / loadPerReplica)`，再钳制到 `[minReplicas, maxReplicas]`。
  - `upWindowSeconds`：扩容看的短窗口（取窗口内峰值，敏感）。
  - `downWindowSeconds`：缩容看的长稳定窗口（取窗口内峰值，保守）。
  - `upThresholdPercent` / `downThresholdPercent`：迟滞带。期望/当前 必须分别
    ≥ `1+上阈值`、≤ `1-下阈值` 才允许动作，阈值附近一律保持。
  - `maxUpStep` / `maxDownStep`：单拍扩/缩的副本数硬限速。
  - `upCooldownSeconds` / `downCooldownSeconds`：距上次同方向动作的冷却。
  - `scaleUpStabilizationSeconds`：扩容后的稳定期，期间禁止缩容（防止刚扩完就缩）。
  - `coldStartDelaySeconds`：初始冷启动窗口；同时也是新副本从扩容到就绪的时间。
  - `freshnessSeconds`：最新样本超过该年龄 → 信号失效，本拍保持。
  - `maxSampleGapSeconds`：缩容窗口允许的最大采样间隔，超过即视为窗口不完整。
- `samples`：升序时间序列；`missing:true` 表示该时刻**显式缺失**（监控系统报不出值），
  与“负载为 0”严格区分。
- 决策时刻二选一：显式 `decisionTimes`（严格升序），或给 `decisionIntervalSeconds`
  由样本时间范围自动生成。
- `initialReplicas`：省略或 0 表示以 `minReplicas` 起步（视为已就绪）。

响应中每个 `decisions[i]` 包含：动作、就绪/冷启动中副本（前后值）、两路窗口峰值与
期望副本、是否被上下限钳制、信号是否存活、缩容窗口是否完整、`reasons`（中文理由链）、
以及 `usedSamples`（该拍可见的全部样本及其窗口归属/新鲜度）。

### 请求样例

```bash
# 健康检查
curl -s http://127.0.0.1:8080/healthz

# 回放尖峰场景
curl -s -X POST http://127.0.0.1:8080/api/v1/replay \
  -H 'Content-Type: application/json' \
  -d @examples/spike-request.json

# 回放含采集中断的振荡场景
curl -s -X POST http://127.0.0.1:8080/api/v1/replay \
  -H 'Content-Type: application/json' \
  -d @examples/oscillation-request.json
```

错误均返回 4xx JSON：非法 JSON/未知字段 `400`、`GET /replay` 返回 `405`、未知路径 `404`；
请求体上限 10 MiB。

---

## 4. 决策流水线（每一拍的判定顺序）

引擎每个决策时刻严格按以下顺序推进，任何一环不通过即**保持**，并记录理由：

1. **冷启动副本就绪**：上一次扩容的副本到达就绪时刻 → 转为就绪。
2. **信号存活（指标缺失 ≠ 零负载）**：
   - 无样本 / 最新样本 `missing:true` / 最新样本年龄 > `freshnessSeconds`
     → 立即保持，不做任何窗口计算。
3. **冷启动门禁**：处于初始冷启动窗口，或仍有副本在冷启动中 → 保持。
4. **窗口期望**：
   - 扩容期望 = 短窗口内非缺失样本的峰值负载换算；
   - 缩容期望 = 长窗口内非缺失样本的峰值负载换算（峰值口径，宁可晚缩）。
5. **缩容窗口完整性保护**：长窗口内若存在显式缺失、采样间隔 > `maxSampleGapSeconds`、
   或左右边界无新鲜样本覆盖，则**拒绝缩容**（扩容不受此限）。
6. **迟滞带**：变化幅度未越过上/下阈值 → 保持，吸收抖动。
7. **冷却 / 扩容后稳定窗口**：扩容冷却、缩容冷却、扩容后稳定期分别拦截。
8. **单拍限速 + 上下限钳制**：扩容最多 `+maxUpStep`（新副本进入冷启动），
   缩容最多 `-maxDownStep`；任何期望都钳在 `[min,max]`。

---

## 5. 三个验收场景与实测结果

以下均为在本机真实运行的结果（`go run ./cmd/scenarios`，go1.23.4）。

### 场景一：突发尖峰（`examples/spike-request.json`）

曲线：t=30/90 两个 900 孤立尖峰落在初始冷启动窗口内；t=300~419 为 850 高负载平台
（期望 9 副本，当前 2）；t=420 起回落到 150。

实测行为：

| 时刻 | 动作 | 验证点 |
|------|------|--------|
| 0s、60s | 保持 | 初始冷启动窗口门禁，900 尖峰被无视 |
| 120s | 扩容 +2（冷启动中 2） | 期望 9，但单拍限速 +2 |
| 180s | 保持 | 新副本冷启动中，不做决策 |
| 240s | （就绪 4）+ 不再动作 | 短窗口峰值已随尖峰过去而回落 |
| 300s/420s | 扩容 +2 | 高平台期间“扩容→冷启动→再扩”节拍，每次最多 +2 |
| 420s 后回落，420~660s | 保持 | 300s 缩容窗口仍含 850 峰值，拒绝缩容（缩容保护） |
| 720s | 缩容 -1 | 回落 300s 后窗口首次完整、且越过 20% 迟滞 |
| 780s | 保持 | 缩容冷却 120s 未到 |
| 840/960/1080/1200/1320s | 各缩 -1 | 每 120s 最多缩 1，单拍限速 -1 |
| 1380s 起 | 保持 | 回到 minReplicas=2，迟滞带守住下限 |

汇总：25 拍，扩容 3 次，缩容 6 次，峰值总副本 8，最终就绪 2。

### 场景二：持续增长后跌落（`examples/ramp-request.json`）

曲线：负载 `100+1.5t` 线性增长，t=1200 达 1900（期望 **19** 副本），随后跌回 100。

实测行为：

- t=120s 起每拍最多 +2、扩容后冷启动 120s，副本按 2→3→5→7→9→10 爬升；
- t=720~1200s 期望 12~19 副本，全部被 **maxReplicas=10 上限钳制**（响应中 `clamped:true`）；
- t=1200 跌落后，1260/1320/1380/1440 四拍长窗口仍含 1900 峰值，**稳定窗口拦截缩容**；
- t=1500s 窗口首次完全低负载，开始缩容，之后严格每 120s 缩 1 个；
- t=2340s 缩到 2，之后期望 1 被 **minReplicas 下限钳制**、迟滞带保持。

汇总：49 拍，扩容 5 次，缩容 8 次，保持 36 次，峰值总副本 10（=上限），最终就绪 2。

### 场景三：周期振荡 + 采集中断（`examples/oscillation-request.json`）

曲线：720s 周期、150~650 的正弦负载；**t∈[600,720) 样本显式缺失**。

实测行为：

- 首个波峰按限速 2→3→5→7 扩容（+1、+2、+2，受冷启动节拍影响）；
- t=600/660：最新样本缺失 → `signalOk=false`、保持 7 副本（**没有按零负载缩容**）；
- t=720~960：信号恢复，但长窗口仍包含缺失段，`downWindowComplete=false`
  → “期望更低也拒绝缩容”；
- 波谷期间（如 t=1380 期望 3、t=1440 期望 6）被 **20% 缩容迟滞带**吸收，不动作；
- t=1500：缺口彻底离开 300s 窗口后，才缩出第一个 -1。

汇总：26 拍，扩容 3 次，缩容 1 次，保持 22 次（其中 2 次为信号失效保持），峰值 7。

### 手工小样例（`examples/manual-request.json`）

冷启动 60s 的紧凑样例，可一次看到：冷启动门禁 → +2 扩容并进入冷启动 →
冷启动完成但恰逢指标缺失（保持，不当零）→ 长窗口仍含高峰值（不缩）→
长窗口包含缺失段（窗口不完整，不缩）。运行：

```bash
go run ./cmd/scaler -addr :18080 &
curl -s -X POST http://127.0.0.1:18080/api/v1/replay \
  -H 'Content-Type: application/json' -d @examples/manual-request.json
```

---

## 6. 自动化测试与实测记录

测试均已实际执行通过（go1.23.4 linux/amd64）：

```text
$ go test ./...
ok  offline-scaler/internal/api      (7 个 HTTP 测试)
ok  offline-scaler/internal/scaler   (13 个单元测试 + 4 个场景/校验测试)

$ go test -cover ./...
offline-scaler/internal/api      coverage: 92.3%
offline-scaler/internal/scaler   coverage: 93.5%

$ go test -race ./...            # 无数据竞争
$ go vet ./...                   # 无告警
```

测试覆盖的关键点：

- 冷启动窗口门禁、冷启动期间拒绝二次决策；
- **显式缺失样本保持、过期样本保持，均不按零负载缩容**；
- 扩容单拍限速 +2、新副本冷启动；缩容单拍限速 -1、缩容冷却 120s；
- 300s 缩容稳定窗口、窗口内缺失/采样空洞/边界不新鲜均判不完整；
- 上/下迟滞带吸振；扩容后稳定窗口禁止缩容；min/max 上下限钳制；
- 决策记录包含所用样本（含窗口归属、新鲜度）且不读取未来样本；
- 三场景的关键节拍断言（首次扩容/缩容时刻、各拍动作、最终副本数）；
- HTTP：正常回放、非法 JSON、未知字段、非法配置、405、404、健康检查。

HTTP 实测（本地 curl）：200/400/404/405 状态码与响应体均符合预期，
缺失指标请求返回 `action=hold, signalOk=false, targetReplicas=3`（保持，未缩容）。

---

## 7. 设计取舍与边界

- **扩容敏感、缩容保守**：扩容用短窗口峰值，缩容用长窗口峰值，且缩容额外要求
  窗口连续完整。两个方向都用峰值而非均值，避免对尖峰欠配。
- **缺失语义三分**：无样本 / 显式 `missing` / 样本过期，统一表现为“信号失效→保持”，
  绝不以 0 负载补洞。
- **离线确定性**：引擎不依赖墙上时钟，全部由输入的时间戳驱动；同一输入结果恒定，
  可直接用于审计和回归测试。
- **时间粒度**：所有时间为整数秒；决策时刻与采样时刻不要求对齐（新鲜度/间隔按秒判定）。
- **零值配置即默认值**：受标准库 JSON 反序列化限制，无法区分“省略”与“显式 0”，
  因此配置中的 0 一律视为“采用默认值”；如需关闭某个冷却/窗口，请显式给极小正值
  （例如 1）。场景配置均显式给出全部参数，不受此约定影响。

## 8. 未完成项 / 不做的事

- 不做任何前端界面（题目明确为纯后端）。
- 不做真正的云平台对接（不创建/删除真实副本），输出的是“决策与目标副本数”；
  接入真实编排系统只需消费每拍的 `action/targetReplicas`。
- 不做多策略/多服务实例并存与持久化：单进程、单回放，无状态存储。
- 未做指标预测（如线性外推）、多指标融合（CPU+QPS 取 max 等）；
  当前为单一总负载指标 + 固定单副本容量。
- 未做鉴权/ TLS（按本地/内网工具定位，需要时可在前置网关增加）。
