# cardinalgov — 基数预算治理后端（纯 Go，无外部依赖）

一个可观测性数据处理后端的最小实现样例：接收指标样本（HTTP），对
**指标 × 标签组合（series）的基数**施加预算治理，把超出预算的高基数组合
聚合到每指标一个的 **overflow 桶**，并提供查询、全局计数、标签长度限制、
快照持久化与重启恢复。全部数据为合成数据，不依赖任何真实监控平台。

## 要解决的问题

监控系统常见故障模式：某个标签（如 `request_id`、`user_id`、`trace_id`）
带上每个请求都不同的值，标签组合数（series cardinality）爆炸，内存随流量
线性增长直至 OOM。本项目演示的治理策略：

- **每指标预算**：每个指标名最多保留 N 个不同标签组合（默认 10000，可按指标覆盖）。
- **Overflow 聚合**：预算用尽后出现的新组合不再分配任何 map 项，其样本值/计数
  累加进固定桶 `{"__bucket__":"overflow"}`。
- **已有组合永不淘汰**：先到的合法组合永久保留，不做 LRU——新标签永远挤不走老组合。
- **全局指标名预算**：不同指标名数量也有上限（`MaxMetricNames`）。
- **标签长度限制**：标签键超长拒绝；标签值超长默认按 UTF-8 边界截断（可改为拒绝）。
- **计数守恒**：任意时刻 `received == accepted + overflowed + rejected`，
  `/stats` 直接返回 `conservation_ok`。

## 目录结构

```
cmd/server/             HTTP 服务入口（含周期快照、优雅退出时落盘）
cmd/synthload/          合成高基数攻击负载生成器
internal/governor/      核心：预算判定、overflow、计数器、快照持久化
  config.go             配置与默认值
  store.go              并发安全存储（sync.RWMutex）
  persist.go            JSON 快照原子写 + 恢复（含预算缩小时折叠）
  *_test.go             单元/并发/攻击夹具/持久化测试
internal/httpapi/       HTTP 接口与端到端测试
examples/               请求样例 JSON
scripts/demo.sh         端到端演示脚本
```

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/ingest` | 摄入单条对象、`{"samples":[...]}` 批量对象，或 JSON 数组 |
| `GET`  | `/metrics` | 指标名列表 |
| `GET`  | `/metrics/{name}` | 单指标的组合明细、预算占用、overflow 桶 |
| `GET`  | `/stats` | 全局计数、指标/组合数、守恒校验 |
| `POST` | `/admin/snapshot` | 立即落盘（未配置快照路径时返回 503） |
| `GET`  | `/healthz` | 存活探针 |

摄入响应：全部成功 `200`；批量中有拒绝项时 `202`（其余照常处理），逐条结果
在 `results` 中给出，拒绝原因汇总在 `errors` 中。

样本格式：

```json
{
  "metric": "http_requests",
  "labels": {"route": "/login", "pod": "pod-0007"},
  "value": 1.0,
  "timestamp": 1790000000000
}
```

`timestamp` 可省略（服务端填 Unix 毫秒）。指标名/标签键为 Prometheus 风格
`[a-zA-Z_:][a-zA-Z0-9_:]*`；`__` 前缀标签键保留给 overflow 桶，用户不能使用。

## 快速开始

```bash
go test ./...                       # 全部测试
go test -race ./...                 # 含竞态检测
bash scripts/demo.sh                # 端到端：攻击 + 守恒 + 重启恢复

go run ./cmd/server \
  -addr=127.0.0.1:8080 \
  -series-budget=10000 \
  -metric-budgets=http_requests=50,errors_total=200 \
  -max-metric-names=512 \
  -max-label-value-bytes=256 \
  -snapshot=./data/state.json \
  -flush-interval=5s

go run ./cmd/synthload -budget=50 -attack=200000
```

所有参数同时支持环境变量（`ADDR`、`SERIES_BUDGET`、`METRIC_BUDGETS`、
`MAX_METRIC_NAMES`、`MAX_LABEL_VALUE_BYTES`、`TRUNCATE_VALUES`、
`SNAPSHOT_PATH`、`FLUSH_INTERVAL`）。

### curl 样例

```bash
curl -s localhost:8080/ingest -H 'Content-Type: application/json' \
  --data @examples/ingest-single.json
curl -s localhost:8080/ingest -H 'Content-Type: application/json' \
  --data @examples/ingest-batch.json
curl -s localhost:8080/stats
curl -s localhost:8080/metrics/http_requests
```

## 持久化与恢复

- 快照为 JSON，写入采用 `tmp 文件 + fsync + rename` 原子替换；另有周期落盘
  和 SIGINT/SIGTERM 优雅退出时落盘。
- 启动时若快照存在则恢复全部组合、overflow 桶与全部计数器。
- 若恢复时预算被改小，超出的既有组合被**折叠进 overflow 桶**
  （SampleCount/ValueSum 累加，样本不丢）。

## 验收点与对应测试

| 验收要求 | 位置 |
|---|---|
| 高基数攻击下内存有界 | `internal/governor/attack_test.go::TestHighCardinalityAttackMemoryBounded`（50 万唯一组合，GC 后堆增量断言） |
| 超限进 overflow、老组合不被挤走 | `TestSeriesBudgetAndOverflow`、攻击夹具中的 stable series 断言 |
| 溢出前后计数守恒 | `Counters.CheckConservation`，所有测试 + `/stats` 中 `conservation_ok` |
| 全局计数 + 标签长度限制 | `TestMetricNameBudget`、`TestLabelValidation`、`TestTruncateUTF8` |
| 并发摄入 | `TestConcurrentIngest`、`httpapi.TestConcurrentHTTPIngest`（`-race` 通过） |
| 重启恢复 | `TestSnapshotRestoreConservation`、`TestRestoreAppendsSurvive`、`TestRestoreFoldWhenBudgetShrunk`、`scripts/demo.sh` |

## 范围与非目标

- 教学/样例性质：单机内存态 + JSON 快照，不是分布式 TSDB，无 WAL、无分片、
  无 TTL/时间窗口聚合、无前端。
- overflow 桶是单一粗粒度桶，不区分被聚合组合的来源标签（这是“有界内存”
  的必然代价；真实系统可在此基础上增加分层/分维度 overflow）。
- HTTP 服务仅明文、无鉴权，适合本地与可信网络演示。
