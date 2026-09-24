# 伸缩稳定窗口（Scaling Stable Window）

HPA 式本地伸缩决策服务：Go + HTTP + SQLite，纯后端。每次决策输出
**原始建议（公式结果）**、**限制后建议（稳定窗口 + min/max 之后）** 与
**原因列表**，并以 HMAC-SHA256 签名、绑定配置版本与指标事件时间。

## 决策算法

### 公式（与 Kubernetes HPA 一致）

```
rawDesired = ceil(currentReplicas × effectiveUtilization / targetUtilization)
```

- `targetUtilization`：目标利用率（0–100，百分数，如 70 表示 70%）。
- 容忍区间：`|util/target − 1| ≤ tolerancePct/100` 时保持当前副本数，
  避免在目标附近来回抖动（flapping）。
- 稳定窗口：缩容（及容忍带内保持）取 **窗口内历史原始建议的最大值**，
  窗口按**指标事件时间**滑动（默认 300s）；扩容窗口默认 0（立即生效，
  可配置为同样取最大值）。
- 最终结果再经 `minReplicas`/`maxReplicas` 截断，原因记录在 `reasons`。

### 三类特殊数据的分别处理（绝不把缺失当 0）

| 情形 | 处理 |
|---|---|
| 缺指标（ready 实例未上报） | 单个缺失按 **100%** 保守计入公式；**全部** ready 实例缺测 → `Hold`，不记录建议（`all_ready_instances_missing_metrics`） |
| 未就绪实例 | 从实测均值中剔除；扩容时按 100%、缩容时按 0% 计入安全权重（K8s HPA 语义） |
| 零目标值 | `targetUtilization=0` 在配置 PUT 时即被拒绝（422 `zero_target_value_rejected`），杜绝除零 |
| 过期样本 | 早于 `metricFreshnessSeconds` 的样本 → `Hold`（`metrics_stale`），不记录建议 |

### 版本与时间不变量

- 每次决策绑定 `configVersion` + `configFingerprint`（策略内容的 SHA-256）
  与 `metricTimestamp`（事件时间）。
- **事件时间回退不得覆盖新建议**：`metricTimestamp` 早于已接受的最新事件
  时间 → 409 `metric_timestamp_regressed`；同一时间戳重放 → 幂等返回原决策。
- 配置版本升级后，稳定窗口只统计新版本的原始建议（窗口随版本重置）。
- 每条决策带 `signature`（HMAC-SHA256，密钥 `SCALER_SIGNING_KEY`），
  可用 `cmd/verify` 真实校验。

## 快速开始

```bash
# 构建并启动（真实时钟）
go build -o scalerd ./cmd/scalerd
./scalerd -addr=:8080 -db=scaler.db

# 或手动时钟（测试/演示）
./scalerd -addr=:8080 -db=scaler.db -manual-clock -clock-start=2026-09-23T12:00:00Z
```

```bash
# 1. 下发配置（返回 version=1 与 SHA-256 指纹）
curl -X PUT localhost:8080/scalers/checkout/config \
  -H 'Content-Type: application/json' -d @examples/config.json

# 2. 决策（阶跃负载：4 副本 × 100% → raw=6）
curl -X POST localhost:8080/scalers/checkout/decide \
  -H 'Content-Type: application/json' -d @examples/decide-step-up.json

# 3. 查询历史与窗口输入
curl localhost:8080/scalers/checkout/decisions
curl localhost:8080/scalers/checkout/recommendations

# 4. 手动时钟（仅 -manual-clock 模式）
curl -X POST localhost:8080/clock/advance -d '{"seconds":300}'
curl localhost:8080/clock

# 5. 校验决策签名（真实 HMAC-SHA256 校验）
go build -o verify ./cmd/verify
curl -s -X POST localhost:8080/scalers/checkout/decide -d @examples/decide-step-up.json \
  | ./verify -key-hex=$SCALER_SIGNING_KEY
```

## 测试与验收

```bash
go test ./...          # 单元测试（公式/容忍带/缺测/未就绪/零目标）+ 集成测试
                       # （阶跃负载、周期波动、长缺测、最大副本限制、事件时间回退）
./scripts/accept.sh    # 端到端验收：真实启动服务，41 项检查，含 HMAC 真实校验
make test              # 等价 go test ./...
make accept            # 等价 ./scripts/accept.sh
```

## HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 健康检查 |
| GET/POST | `/clock`, `/clock/set`, `/clock/advance` | 可控时钟（手动模式） |
| PUT | `/scalers/{id}/config` | 新建配置版本（单调递增） |
| GET | `/scalers/{id}/config` / `/configs` | 当前/全部配置版本 |
| POST | `/scalers/{id}/decide` | 决策（见下） |
| GET | `/scalers/{id}/decisions` | 决策审计历史 |
| GET | `/scalers/{id}/recommendations` | 稳定窗口输入（原始建议） |

### 决策请求

```json
{
  "configVersion": 1,
  "currentReplicas": 4,
  "metricTimestamp": "2026-09-23T12:00:00Z",
  "instances": [
    {"name": "p0", "ready": true,  "utilizationPct": 100},
    {"name": "p1", "ready": true,  "utilizationPct": null},
    {"name": "p2", "ready": false}
  ]
}
```

`utilizationPct` 省略/`null` 表示**缺测**（不是 0）；`ready:false` 表示未就绪。

### 决策响应（节选）

```json
{
  "configVersion": 1,
  "configFingerprint": "3b0c53d9…",
  "metricTimestamp": "2026-09-23T12:00:00Z",
  "currentReplicas": 4,
  "rawDesired": 6,            // 原始建议（公式）
  "rawAction": "ScaleUp",
  "windowedDesired": 6,       // 稳定窗口后的建议
  "finalDesired": 6,          // 限制后建议（min/max 截断）
  "finalAction": "ScaleUp",
  "reasons": ["raw_scale_up", "no_stable_window"],
  "recommendationRecorded": true,
  "instanceDetails": [ … ],   // 每个实例如何被计入
  "signature": "161ed7c5…"    // HMAC-SHA256
}
```

### 错误码

| 状态 | reason | 含义 |
|---|---|---|
| 409 | `metric_timestamp_regressed` | 事件时间回退，拒绝覆盖新建议 |
| 409 | `config_version_reset_clears_window` | 请求携带的配置版本不是当前版本 |
| 422 | `zero_target_value_rejected` | 目标利用率为 0 |
| 422 | `invalid_request` | 实例数与 currentReplicas 不符等 |
| 422 | `metric_timestamp_future` | 事件时间在未来超过 60s |
| 404 | `config_not_found` | 未配置该 scaler |

## 项目结构

```
cmd/scalerd/            服务入口（flag/env 配置）
cmd/verify/             决策签名校验工具
internal/clock/         可控时钟（real/manual）
internal/hpa/           决策引擎：公式、容忍带、稳定窗口、指纹与签名
internal/store/         SQLite 持久化（configs/recommendations/decisions/meta）
internal/api/           HTTP 层
tests/integration/      端到端场景测试（httptest + 真实 SQLite + 手动时钟）
examples/               示例输入
scripts/accept.sh       端到端验收脚本（41 项检查）
```

## 设计说明

- **事件时间驱动**：稳定窗口按 `metricTimestamp` 滑动，而非墙钟；时钟仅用于
  过期判断与 `decidedAt` 审计。手动时钟使测试完全确定。
- **缺测不补零**：缺失值绝不填 0（会把利用率拉低导致错误缩容）；单实例缺失
  按 100% 保守处理，全缺失直接 Hold。
- **窗口取最大**：缩容时取窗口内最大原始建议，保证一次保守的高建议在窗口
  期内持续生效，消除抖动。
- **版本绑定**：配置内容指纹（SHA-256）+ 单调版本号；新版本自动开启新窗口，
  旧建议不会污染新策略。
- **密码学真实执行**：指纹用 SHA-256，决策签名用 HMAC-SHA256，
  `scripts/accept.sh` 会用独立工具做真实校验（含篡改/错误密钥反例）。
