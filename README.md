# Scaling Stable Window（伸缩稳定窗口）

一个 HPA 风格的**本地伸缩决策服务**：纯后端（Go + HTTP + SQLite），根据实例利用率给出副本建议，用**目标利用率公式**计算原始建议、用**容忍区间**防抖、用**稳定窗口内最大建议**抑制缩容抖动。每次决策都绑定**配置版本**与**指标时间**，乱序/回退的事件时间不会覆盖新建议；每条决策带真实的 **HMAC-SHA256 签名**，可在线校验。

- 语言/运行时：Go 1.22（CGO，`mattn/go-sqlite3` v1.14.24，已在 go.sum 锁定）
- 存储：SQLite（WAL 模式）
- 无前端、无外部编排器依赖；时间通过 `internal/clock` 抽象，测试用可控假时钟驱动

---

## 1. 决策模型（算法）

### 1.1 目标利用率与建议副本公式

只统计 **Ready 且上报了指标** 的 Pod，取其利用率算术平均：

```
avgUtilization = Σ(ready 且有指标 Pod 的利用率) / 上报指标的 Ready Pod 数
ratio          = avgUtilization / targetPct
rawProposed    = ceil(ratio × currentReplicas)        // 带容忍度取整，见 1.3
```

- 扩容（`rawProposed > currentReplicas`）：**立即生效**，不等稳定窗口。
- 缩容（`rawProposed ≤ currentReplicas`）：取**稳定窗口 `[now-stableWindowSec, now)` 内所有原始建议的最大值**。窗口左闭右开：恰好落在窗口左边界的建议仍计入，当前时刻自己的建议不计入。
- 窗口内没有任何历史原始建议时：保守地**保持当前副本**（不盲目缩容）。
- 最终结果再做边界裁剪：`final = clamp(stabilized, minReplicas, maxReplicas)`。

输出三者分离：`rawProposed`（原始公式建议）、`stabilized`（窗口处理后建议）、`finalReplicas`（裁剪后可执行建议），并附 `reasons` 原因码，便于审计。

### 1.2 容忍区间（防抖动死区）

当 `|ratio − 1| ≤ tolerancePct/100` 时认为在目标附近，**保持当前副本**，且**不向稳定窗口写入建议**（与上游 HPA 行为一致：死区内没有建议可记录）。默认 `tolerancePct = 10`，即 ratio ∈ [0.9, 1.1] 不动。

### 1.3 三类异常输入分别处理（绝不把缺失补成 0）

| 情况 | 处理 |
|---|---|
| **缺指标**（Ready Pod 的 `utilizationPct` 为 `null`） | 该 Pod 计入 `readyMissing` 但**不参与平均**；若所有 Ready Pod 都缺指标 → `ALL_METRICS_MISSING`，保持当前副本，`rawProposed` 为空（不产生建议） |
| **实例未就绪**（`ready: false`） | 完全排除在平均与计数之外（即使带 `0` 也不用，避免冷启动实例把平均值稀释），原因码 `UNREADY_INSTANCES_EXCLUDED` |
| **目标利用率为 0** | 配置即非法，返回 `ZERO_TARGET` 错误（拒绝除零，绝不产生 Infinity 建议） |

> 关键原则：缺失就是缺失。`3 个 Ready Pod，利用率 80/60/null` 的平均是 `70`（按 2 个上报者算），而不是把 null 当 0 得到 `46.67`。

取整使用容忍度感知的 `ceil(x − tolerance)`，避免 `5.9999999…` 这类浮点噪声被错误进到 6。

### 1.4 配置版本与事件时间

- 每次 PUT 配置都对规范化配置 JSON 计算 **SHA-256**，生成确定性版本号 `cfg_<前16位hex>`；配置任一字段变化 → 版本变化。
- 每条决策持久化其 `configVersion` 与 `metricTimeMs`。
- 稳定窗口按 **(workload, configVersion)** 隔离：改配置后从空窗口重新开始。
- **事件时间回退守卫**：新决策的指标时间必须**严格大于**该 workload 已存最新指标时间，否则返回 `409 STALE_METRIC_TIME` 并回带已有决策；相等时间（重复事件）同样拒绝。乱序事件**永远不会覆盖**更新的建议。
- 每条决策对规范化字段串计算真实 **HMAC-SHA256**（hex），可通过 verify 端点重算校验；篡改任一字段即失效。

原因码：`ALL_METRICS_MISSING`、`UNREADY_INSTANCES_EXCLUDED`、`PARTIAL_METRICS_PRESENT`、`WITHIN_TOLERANCE`、`RAW_ROUNDED_UP`、`SCALE_UP_IMMEDIATE`、`STABLE_WINDOW_SCALE_DOWN_MAX`、`STABLE_WINDOW_NO_PROPOSAL_HOLD`、`CAPPED_AT_MAX_REPLICAS`、`CLAMPED_AT_MIN_REPLICAS`。

---

## 2. HTTP 协议

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 健康检查（真实 ping SQLite） |
| PUT | `/api/v1/workloads/{name}/config` | 创建/更新配置，返回 `configVersion` |
| GET | `/api/v1/workloads/{name}/config` | 读取当前配置 |
| POST | `/api/v1/workloads/{name}/decisions` | 提交一个时刻的 Pod 快照，返回决策（201）；事件时间回退返回 409 |
| GET | `/api/v1/workloads/{name}/decisions/latest` | 最新一条决策 |
| GET | `/api/v1/workloads/{name}/decisions` | 决策历史（新→旧） |
| POST | `/api/v1/workloads/{name}/decisions/verify` | 重算 HMAC，校验决策签名 `{ "valid": true|false }` |

配置请求体：

```json
{ "minReplicas": 1, "maxReplicas": 10, "targetPct": 50, "tolerancePct": 10, "stableWindowSec": 60 }
```

决策请求体（`metricTime` 为 RFC3339；缺指标用 `null`，不是 0）：

```json
{
  "metricTime": "2026-09-23T10:00:30Z",
  "currentReplicas": 8,
  "pods": [
    { "name": "web-a", "ready": true,  "utilizationPct": 18.75 },
    { "name": "web-g", "ready": false, "utilizationPct": 0 },
    { "name": "web-c", "ready": true,  "utilizationPct": null }
  ]
}
```

决策响应（节选）：`rawProposed`、`stabilized` 为 `null` 表示该阶段无建议；`windowUsed` 给出参与取最大值的窗口点。

```json
{
  "workload": "demo-web", "configVersion": "cfg_c924911a37e0c2c7",
  "metricTime": "2026-09-23T10:00:30Z", "metricTimeMs": 1790157630000,
  "currentReplicas": 8, "readyCount": 7, "unreadyCount": 1,
  "readyReporting": 5, "readyMissing": 2,
  "avgUtilizationPct": 18.75, "metricPresent": true,
  "targetPct": 50, "ratio": 0.375,
  "rawProposed": 3, "stabilized": 8, "finalReplicas": 8, "action": "hold",
  "reasons": ["UNREADY_INSTANCES_EXCLUDED", "PARTIAL_METRICS_PRESENT",
              "RAW_ROUNDED_UP", "STABLE_WINDOW_SCALE_DOWN_MAX"],
  "signature": "110c6b06…b4bf"
}
```

---

## 3. 本地启动

需要 Go 1.22+ 与可用的 C 编译器（CGO，go-sqlite3 要求）。

```bash
# 构建
go build ./...

# 启动（默认 :8090，数据库 data/scaler.db）
export SCALER_HMAC_SECRET="$(openssl rand -hex 32)"   # 不设则用明确标注的开发默认值并打印警告
go run ./cmd/server -addr :8090 -db data/scaler.db
# 可选: -db :memory: 表示内存库（进程退出即清空）
```

启动后：

```bash
curl -s localhost:8090/healthz
curl -s -X PUT localhost:8090/api/v1/workloads/web/config \
  -H 'Content-Type: application/json' -d @examples/config.json
```

示例输入见 [`examples/`](examples/)。

### 一键演示（自动起服务、跑阶跃/缺测/乱序场景、再关闭）

```bash
./scripts/demo.sh
```

---

## 4. 验收命令

```bash
# 全部自动化测试（纯算法 + SQLite + HTTP 端到端）
go test ./... -count=1 -v

# 竞态检测
go test -race ./... -count=1

# 依赖完整性
go mod verify
```

测试覆盖（均通过假时钟 `internal/clock` 精确控制时间）：

- **阶跃负载**：低负载→负载翻倍立即扩容；负载骤降后被稳定窗口挡住（t=40s/70s 仍保持 8），窗口最大建议老化后（t=80s）才缩到 3。
- **周期波动**：60s 周期振荡 240s，断言任何缩容建议都不低于窗口内最大原始建议，峰值立即到达、谷值滞后窗口。
- **长缺测**：连续 90s 缺指标始终保持且不写窗口；指标恢复后先因空窗口保守保持一拍，再缩容；验证缺失绝不补 0。
- **最大副本限制**：ratio=4 → 原始 40，裁剪到 maxReplicas=5，原因 `CAPPED_AT_MAX_REPLICAS`。
- **缺指标 / 未就绪 / 部分指标**：平均值口径与原因码。
- **事件时间回退**：更旧与相等时间均被 409 拒绝，最新建议不被覆盖（含 SQLite 重启后仍生效）。
- **配置版本绑定**：决策绑定 SHA-256 版本；改版本后窗口隔离、从空窗口开始。
- **密码操作**：SHA-256 版本号、HMAC-SHA256 签名/验签/篡改失败，均用标准库真实计算并有独立手工 HMAC 对照。

---

## 5. 目录结构

```
cmd/server/            服务入口（标志、优雅关停、HMAC 密钥）
internal/scaler/       纯决策算法（无 I/O、无墙钟，便于可控时钟测试）
internal/clock/        时间抽象（System / Fake）
internal/store/        SQLite：配置、配置版本、决策、窗口建议、回退守卫
internal/crypto/       SHA-256 配置版本 + HMAC-SHA256 决策签名
internal/httpapi/      HTTP 协议层 + httptest 端到端测试
examples/              示例请求 JSON
scripts/demo.sh        一键端到端演示
```

## 6. 设计取舍说明

- **稳定窗口只存“原始建议”**且仅在死区之外记录，这是上游 Kubernetes HPA 的成熟语义；缩容取窗口最大值，扩容绕过窗口，二者共同消除抖动。
- **空窗口保守保持**：服务刚启动或刚改配置、没有足够历史时不做激进缩容。
- **写串行化**：SQLite 连接池设为单连接并配合进程内 RWMutex，使“读最新时间→校验→插入决策→写窗口”在事务内原子完成，杜绝并发覆盖。
- 时间字段同时提供 RFC3339 字符串与毫秒数；签名覆盖规范化字段串（版本、指标时间、三类建议、动作、原因等），验签为常量时间比较。
