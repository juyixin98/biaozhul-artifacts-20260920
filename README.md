# 告警迟滞状态机（Alert Hysteresis State Machine）

一个纯后端的可观测性数据处理样例：接收合成监控样本，按阈值规则做
**持续触发、恢复迟滞、缺数（no-data）判定**，全部由一个**只进不退的虚拟时钟**
驱动；提供 HTTP 摄入/查询接口和本地 JSON 文件持久化。无前端、无真实监控平台依赖。

- 语言：Go 1.22，仅用标准库
- 通知（firing / resolved / nodata / data_resumed）**只在状态真正转换时产生一次**
- 重复样本（同 metric+timestamp）不累计持续时长
- 修改规则配置：版本号 +1，运行时状态**明确重置**，并记录 `rule_reset` 审计事件
- 迟到/乱序样本：存储可查，但永不回拨时钟、永不重放状态机

---

## 1. 状态机

```
                 热样本(hot)持续≥trigger_for
        ┌──────────────────────────────────────┐
        │                                       ▼
 ┌─────────────┐  hot        ┌─────────┐  sustained  ┌────────┐
 │  inactive   │ ──────────▶ │ pending │ ──────────▶ │ firing │
 └─────────────┘             └─────────┘             └────────┘
        ▲                       │ warm/cold            │ warm(在迟滞带内,保持)
        │                       └──▶ inactive          │ cold
        │                                              ▼
        │ resolved(冷持续≥recover_for)         ┌─────────────┐
        └─────────────────────────────────────│ recovering  │
                                              └─────────────┘
        任意状态 ──(距最后样本 ≥ no_data_for，由 tick 驱动)──▶ nodata
        nodata  ──(收到新样本)──▶ data_resumed → 按值进入 inactive/pending（可链式触发）
```

样本值被分为三档（针对 `>` / `>=` 类规则）：

| 档位 | 条件（以上行规则为例） | 作用 |
|---|---|---|
| hot  | `v ≥ threshold` | 计入/延续“热持续” |
| warm | `recovery_threshold < v < threshold`（配置了 `recovery_threshold` 时） | **迟滞带**：告警保持开启、恢复倒计时清零；pending 阶段则直接回落 inactive |
| cold | `v ≤ recovery_threshold`（未配置迟滞带时，非 hot 即 cold） | 计入“冷持续/恢复倒计时” |

对 `<` / `<=` 类下行规则，迟滞带方向相反（`recovery_threshold` 必须严格大于阈值）。

**为什么抖动不会误报**：

- 时长迟滞：hot 持续时间未满 `trigger_for` 不触发；warm 会打断 pending。
- 数值迟滞：firing 后值落进迟滞带（warm）不恢复，只有持续 cold 满 `recover_for` 才 resolve。
- recovering 期间重新 hot：立即回到 firing，**但不重复发 firing 通知**（告警从未真正恢复）。

## 2. 时间语义（重点）

- 系统时间只有一个**虚拟时钟**，初始固定为 `2026-01-01T00:00:00Z`（可复现）。
- 时钟推进的两种方式：
  - 摄入样本的 `ts` 晚于当前时钟 → 时钟跳到该 `ts` 并求值；
  - 调用 `/clock/tick` 或 `/clock/tick-to`（缺数检测靠它在“没有样本”时推进）。
- **重复样本不累计时长**：同一 `metric + timestamp` 的样本只更新存储值，热/冷持续锚点不变。
  持续时长按虚拟时钟计算（`now - hot_since`），而不是按样本条数。
- **时间倒序**：`ts < 当前时钟` 的样本标记 `late=true`，照常入库可查询，但不参与求值、
  不产生任何事件，时钟也绝不回退。
- 一个请求里的批量样本会先去重（同 metric+ts 后者覆盖）再**按时间升序**应用，
  所以乱序提交与顺序提交结果一致。
- 样本时间戳统一截断到秒级精度（UTC）。

## 3. 目录结构

```
.
├── go.mod
├── cmd/
│   ├── server/main.go          # HTTP 服务 + 启动时快照恢复
│   └── synth/main.go           # 合成数据生成器（4 个场景）
├── internal/
│   ├── engine/                 # 纯状态机内核（无 HTTP/IO 依赖，便于单测）
│   │   ├── types.go            # 状态、事件类型、操作符、时间/时长 JSON 类型
│   │   ├── rule.go             # 规则配置、三档分类、校验
│   │   ├── engine.go           # 虚拟时钟与状态转换核心
│   │   ├── manage.go           # 规则 CRUD、摄入、查询、快照/恢复
│   │   └── engine_test.go      # 状态机单元测试
│   ├── persist/
│   │   ├── filestore.go        # 原子写 JSON 快照（temp+fsync+rename）
│   │   └── filestore_test.go
│   └── httpapi/
│       ├── server.go           # HTTP 路由与 DTO
│       └── server_test.go      # HTTP 端到端测试
├── examples/requests.sh        # 可直接运行的 curl 走查脚本
└── data/                       # 运行后生成的快照（默认 ./data/snapshot.json）
```

## 4. 构建与运行

```bash
go build ./...
go run ./cmd/server -addr 127.0.0.1:28080 -db ./data/snapshot.json
```

启动后日志会显示虚拟时钟起点；若快照存在则自动恢复。

生成合成数据（另开一个终端）：

```bash
go run ./cmd/synth -url http://127.0.0.1:28080 -scenario jitter     # 阈值附近抖动
go run ./cmd/synth -url http://127.0.0.1:28080 -scenario gap         # 触发后长时间缺数→恢复
go run ./cmd/synth -url http://127.0.0.1:28080 -scenario outoforder  # 乱序批次 + 迟到样本
go run ./cmd/synth -url http://127.0.0.1:28080 -scenario band        # 数值迟滞带
```

手动 curl 走查：

```bash
bash examples/requests.sh
```

## 5. HTTP API

所有响应为统一信封：`{"ok": true, "data": ...}` 或 `{"ok": false, "error": {...}}`。
时间字段接受 RFC3339 字符串或 Unix 秒数字；时长接受 `"60s"`、`"2m"` 等字符串。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET  | `/health` | 健康检查 + 当前虚拟时钟 |
| GET  | `/clock` | 查看虚拟时钟 |
| POST | `/clock/tick` | `{"duration":"5m"}` 推进时钟并求值（缺数检测） |
| POST | `/clock/tick-to` | `{"to":"2026-01-01T01:00:00Z"}` 跳到指定时间（不可回退） |
| POST | `/rules` | 创建规则 |
| GET  | `/rules` / `/rules/{id}` | 列出/查看规则及运行时状态 |
| PUT  | `/rules/{id}` | **替换配置：版本+1、状态重置、产生 rule_reset** |
| DELETE | `/rules/{id}` | 删除规则（原始样本保留） |
| POST | `/samples` | 批量摄入样本 |
| GET  | `/samples/{metric}?from=&to=&limit=` | 查询原始样本（新到旧，含迟到样本） |
| GET  | `/states` | 所有规则当前状态 |
| GET  | `/events?rule_id=&since=&all=true` | 事件流；默认只含通知，`all=true` 含 rule_reset 审计 |

### 创建规则示例

```json
POST /rules
{
  "id": "cpu1",
  "metric": "cpu_usage",
  "operator": ">=",
  "threshold": 80,
  "trigger_for": "60s",
  "recover_for": "90s",
  "no_data_for": "2m",
  "recovery_threshold": 70
}
```

字段说明：

- `operator`：`>`、`>=`、`<`、`<=`。
- `trigger_for` / `recover_for`：持续触发 / 持续恢复时长；`0` 表示立即。
- `no_data_for`：距最后一个样本超过该时长即进入 `nodata`（必填为正，默认 2m）。
- `recovery_threshold`：可选，启用数值迟滞带。上行规则必须 `< threshold`，
  下行规则必须 `> threshold`。

### 摄入样本示例

```json
POST /samples
{
  "samples": [
    {"metric": "cpu_usage", "ts": 1767225620, "value": 92.5},
    {"metric": "cpu_usage", "ts": "2026-01-01T00:01:00Z", "value": 76}
  ]
}
```

每条结果标记 `accepted` / `late` / `duplicate` / `overwrote`，并内联返回该样本
触发的转换事件。

## 6. 事件类型

| type | 含义 | 是否默认通知 |
|---|---|---|
| `firing` | 热持续满足，进入 firing | 是 |
| `resolved` | 冷持续满足，回到 inactive | 是 |
| `nodata` | 缺数超时 | 是 |
| `data_resumed` | nodata 后重新收到样本 | 是 |
| `rule_reset` | 规则被 PUT 更新（审计用，带新版本号） | 否（需 `all=true`） |

进入/离开 `pending`、`recovering` 这类中间态**不产生通知**。事件含全局递增
`seq`、`from`/`to`、虚拟时间 `ts` 和规则 `version`。

## 7. 持久化

- 每次状态变更后把完整快照原子写入单个 JSON 文件（临时文件 + fsync + rename），
  崩溃不会留下半截文件。
- 启动时若快照存在则恢复：虚拟时钟、规则（含版本）、运行时状态、样本、事件序列。
- 优雅退出（SIGTERM/SIGINT）时再写一次快照。
- 这是“本地持久化样例”，单文件、单机，不适合多副本并发；如需扩展可替换
  `internal/persist` 的实现而不动引擎。

## 8. 测试

```bash
go vet ./...
go test -race -count=1 ./...
```

覆盖的验收场景（`internal/engine/engine_test.go` 等）：

- `TestThresholdJitter`：阈值附近反复抖动不触发；持续触发；warm 保持；warm 打断
  恢复倒计时；持续 cold 才恢复；全程只有 firing/resolved 各一条通知。
- `TestLongNoData` / `TestNoDataBeforeAnySample`：临界时刻缺数转换、持续静默不重复
  通知、健康数据恢复。
- `TestDuplicateSamplesDoNotAccumulate`：同一时间戳重复样本不累计时长；同 ts 覆盖
  不同值的语义；虚拟时间由时钟而非样本条数推进。
- `TestOutOfOrderSamples`：迟到样本入库但不求值、时钟不回退；乱序批次按升序应用。
- `TestNotificationsOnlyOnTransitions`：持续 hot 不重复通知；recovering→firing 不重发。
- `TestRuleUpdateResetsState`：PUT 后版本+1、状态清空、单条 rule_reset、不污染通知流。
- `TestDownwardOperator`、`TestImmediateDurations`、`TestMultipleRulesSameMetric`、
  `TestValidationErrorCases`：下行规则迟滞带、`0` 时长立即语义、同 metric 多规则、
  非法配置拒绝。
- `internal/persist`：快照往返恢复、原子写无残留临时文件。
- `internal/httpapi`：完整 HTTP 端到端（含 400/404、乱序、重复、缺数、配置重置）。

## 9. 设计取舍

- **单进程 + 互斥锁**：状态机内核用一把 `sync.Mutex` 保证一致性，HTTP 层无额外并发
  假设；快照为单文件。样例优先可读性与确定性，而非吞吐。
- **样本内存保留上限**：每个 metric 保留最新 1000 条、事件保留最新 2000 条，防止
  长跑无限增长（均可在引擎常量中调整）。
- **求值在摄入点与 tick 点发生**：系统不在后台按墙钟运行任何东西；不推进虚拟时钟，
  就什么都不会发生——这让测试与演示完全确定、可重放。
