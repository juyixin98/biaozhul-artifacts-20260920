# 带迟滞的离线扩缩容控制器（autoscaler）

纯后端、仅使用 Go 标准库（`net/http`）实现的离线扩缩容控制器。它根据时间序列指标计算目标副本数，并施加冷启动排除、稳定窗口、冷却时间、每步限速和上下限保护。**指标缺失一律视为"未知"而非零负载**——缺失指标的 pod 会被排除在平均值之外；全部指标不可用时保持当前副本数不变。

"离线"意味着控制器自己不看时钟、不连集群：调用方通过 HTTP 上报指标（`POST /v1/metrics`）并显式触发评估（`POST /v1/evaluate`，可指定评估时刻），因此每次决策都是确定、可重放、可审计的。

## 依赖与启动

- 依赖：**Go ≥ 1.22**（开发验证用的是 Go 1.23.4）。无第三方依赖，`go.mod` 即锁定文件（仅标准库，无需 `go.sum`）。
- 运行测试：`go test ./...`
- 启动服务：

```bash
go run ./cmd/autoscaler -addr :8080 -replicas 2
# 或
go build -o autoscaler ./cmd/autoscaler && ./autoscaler -addr :8080 -replicas 2
```

- 运行验收场景（另起一个终端，服务已在 `:8080` 监听）：

```bash
./examples/run_scenarios.sh http://localhost:8080
```

## 算法

每次 `Evaluate(now)` 依次执行：

1. **样本筛选**：每个 pod 取最新样本。样本缺失或超出 `metrics_window_seconds` 的 pod、以及年龄小于 `cold_start_seconds` 的冷启动 pod，全部被排除并记录原因，**不按零负载计入**。没有任何可用样本时决策为 `skip`，副本数不变。
2. **原始推荐**：`ratio = (可用 pod 的平均负载) / target_per_replica`。若 `|ratio - 1| ≤ tolerance`（迟滞带）则维持不变；否则 `raw = ceil(当前副本数 × ratio)`。
3. **稳定窗口**：扩容取 `scale_up_stabilization_seconds` 窗口内推荐的最大值；缩容取 `scale_down_stabilization_seconds`（通常较长）窗口内推荐的最大值——即只缩到近期仍有需求的水平，防止抖动。缩容评估的结果被限制不超过当前副本数（不会由缩容评估变成扩容）。
4. **冷却与限速**：距上次同方向动作不足冷却时间则保持不动；单次动作最多增加 `scale_up_max_step` / 减少 `scale_down_max_step` 个副本。
5. **上下限**：钳制到 `[min_replicas, max_replicas]`。

每次决策都完整记录：所用的样本（`samples_used`）、被排除的 pod 及原因（`excluded`）、使用率、原始/稳定后推荐值、以及人类可读的完整理由链（`reasons`）。

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 健康检查 |
| GET | `/v1/config` | 查看当前配置 |
| PUT | `/v1/config` | 整体替换配置（带校验） |
| POST | `/v1/metrics` | 上报指标样本 |
| POST | `/v1/evaluate` | 触发一次评估（可指定时刻） |
| GET | `/v1/state` | 当前副本数、配置、最近扩缩容时间 |
| GET | `/v1/decisions` | 全部决策历史（含样本与理由） |
| POST | `/v1/reset` | 清空状态并设定副本数（用于重放场景） |

### 请求样例

上报指标（`pod_start` 用于冷启动判断；省略时按样本时刻计，即视为冷启动）：

```bash
curl -X POST localhost:8080/v1/metrics -d '{"samples":[
  {"time":"2026-09-24T12:00:00Z","pod":"p1","pod_start":"2026-09-24T10:00:00Z","value":250},
  {"time":"2026-09-24T12:00:00Z","pod":"p2","pod_start":"2026-09-24T10:00:00Z","value":250}
]}'
# => {"ingested": 2}
```

触发评估（`time` 省略则用服务器时钟）：

```bash
curl -X POST localhost:8080/v1/evaluate -d '{"time":"2026-09-24T12:00:00Z"}'
```

响应（一次真实运行的输出，默认配置：目标 50/pod、限速 +2、扩容冷却 60s）：

```json
{
  "seq": 1,
  "time": "2026-09-24T12:00:00Z",
  "action": "scale-up",
  "current_replicas": 2,
  "desired_replicas": 4,
  "usage_ratio": 5,
  "raw_desired": 10,
  "stabilized_desired": 10,
  "samples_used": [
    {"pod": "p1", "value": 250, "sample_time": "2026-09-24T12:00:00Z"},
    {"pod": "p2", "value": 250, "sample_time": "2026-09-24T12:00:00Z"}
  ],
  "excluded": null,
  "reasons": [
    "usage ratio 5.000 (avg 250.0 / target 50.0 over 2 pods) outside tolerance ±0.10: raw desired = ceil(2 x 5.000) = 10",
    "scale-up rate limit: wanted +8, capped to +2 per action",
    "scaling up 2 -> 4"
  ]
}
```

修改配置：

```bash
curl -X PUT localhost:8080/v1/config -d '{
  "min_replicas":1,"max_replicas":10,"target_per_replica":50,"tolerance":0.1,
  "metrics_window_seconds":60,"cold_start_seconds":30,
  "scale_up_stabilization_seconds":0,"scale_down_stabilization_seconds":300,
  "scale_up_cooldown_seconds":60,"scale_down_cooldown_seconds":120,
  "scale_up_max_step":2,"scale_down_max_step":1
}'
```

## 默认配置

| 参数 | 默认值 | 含义 |
|---|---|---|
| min/max_replicas | 1 / 10 | 副本数上下限 |
| target_per_replica | 50 | 每副本目标负载（与样本同单位） |
| tolerance | 0.1 | 迟滞带 ±10% |
| metrics_window_seconds | 60 | 样本有效期 |
| cold_start_seconds | 30 | 冷启动排除期 |
| scale_up/down_stabilization_seconds | 0 / 300 | 扩容/缩容稳定窗口 |
| scale_up/down_cooldown_seconds | 60 / 120 | 扩容/缩容冷却 |
| scale_up/down_max_step | 2 / 1 | 单次动作最大步长（限速） |

## 实测结果

### 自动化测试（2026-09-24，Go 1.23.4，`go test ./...`）

全部通过：11 个用例，覆盖尖峰限速与冷却、持续增长阶梯扩容、周期振荡防抖动、缺失指标不计零、冷启动排除、缩容稳定窗口保护、上下限钳制、迟滞带、决策审计、配置校验、HTTP 端到端流程。

```
ok  autoscaler/internal/api     0.009s
ok  autoscaler/internal/scaler  0.003s
```

### 场景演练（`examples/run_scenarios.sh`，实际运行输出摘要）

**场景 A：尖峰** —— 2 副本突遇 5 倍负载（ratio=5，raw=10）：

| 时刻 | 动作 | 副本 | 关键理由 |
|---|---|---|---|
| 12:00:00 | scale-up | 2→4 | 限速：想 +8，每次最多 +2 |
| 12:00:30 | hold | 4 | 扩容冷却（30s < 60s） |
| 12:01:01 | scale-up | 4→6 | 冷却结束，再次 +2 |
| 12:02:01 | hold | 6 | 负载崩塌（ratio=0.2）但缩容稳定窗口（300s）保护 |
| 12:07:01 | scale-down | 6→5 | 窗口已过，限速每次 -1 |

**场景 B：持续增长** —— 每 pod 持续 140（ratio=2.8）：2→4→6→8→10，阶梯式追升并在 max=10 处钳制。

**场景 C：周期振荡** —— 总负载在 180↔80 间交替：首次高峰扩容到 4 后，低谷被缩容稳定窗口拦下、次高峰落入迟滞带（ratio=0.9），5 次评估中仅 1 次动作，无抖动。

## 项目结构

```
cmd/autoscaler/main.go        服务入口
internal/scaler/              控制器核心（类型、算法、单元测试）
internal/api/                 HTTP 层（路由、JSON、端到端测试）
examples/run_scenarios.sh     三个验收场景脚本
```

## 已知限制 / 未完成项

- 状态全部在内存中，重启即丢失；未做持久化。
- 单实例、无认证鉴权，仅适合内网/离线评估用途。
- 限速只支持固定步长，未实现"按百分比"限速模式。
- 指标模型为"每 pod 一个最新值"，未实现多样本聚合（如 p95）、多指标（CPU+自定义）加权。
- 控制器只输出决策并更新自身状态，不对接真实编排系统（Kubernetes 等）执行扩缩容。
- 评估假设时间单调前进；对乱序时间戳未做防御。
