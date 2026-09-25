# 基数预算治理（Cardinality Budget Governance）

纯后端的可观测性数据处理样例：用 Go 实现一个带**指标标签组合基数预算**的摄入与查询服务，
使用合成数据、本地 JSON 快照持久化，不依赖任何真实监控平台。

核心目标：防止“高基数攻击”（每个样本带唯一 `request_id` / `trace_id` / `session`）
把内存撑爆，同时**不丢样本、不挤走已有组合**。

## 设计要点

### 1. 每个指标有固定的标签组合预算

- 每个 metric 最多保留 `max-series` 个不同的标签组合（series）。
- 预算用尽后，**新出现的组合全部聚合到该指标唯一的 overflow 桶**（计数与求和累加）。
- **已有组合永不被新标签挤走**（不是 LRU 淘汰）：旧组合再来仍然命中原序列并就地累加。
- overflow 桶与正常序列分开存储、分开查询，计数完全可核对。

### 2. 全局有界

不只是“每指标序列数”有界，所有进入留存态的维度都有硬限制：

| 维度 | 默认值 | 超限行为 |
|---|---|---|
| 每指标标签组合数 `max-series` | 1000 | 新组合 → overflow 桶 |
| 指标名数量 `max-metrics` | 1000 | 拒绝（400，`metric_name_budget_exceeded`） |
| 指标名长度 | 256 字节 | 拒绝 |
| 每样本标签数 | 20 | 拒绝 |
| 标签键长度 | 128 字节 | 拒绝 |
| 标签值长度 | 512 字节 | **按 rune 边界截断**，样本照收 |

由此可由配置推出留存数据的**硬上界**（见 `/api/v1/stats` 的 `logical_bytes_bound`），
与输入流量大小无关。

### 3. 计数守恒

以下恒等式在摄入前后始终成立（测试中逐条断言）：

```
samples_received = samples_accepted + samples_rejected
samples_accepted = normal_samples + overflow_samples
metric.count      = Σ series.count（含 overflow 桶）
全局 accepted     = Σ 各 metric.count
```

### 4. 持久化与重启恢复

- 快照为单个 JSON 文件（`<data-dir>/snapshot.json`），**临时文件 + fsync + rename 原子写入**。
- 启动时若存在快照则恢复全部序列、overflow 桶与全局计数器；恢复时校验配置一致性与计数守恒，
  快照损坏（计数对不上、序列重复、超预算）直接拒绝启动。
- 默认每 5 秒自动快照；`POST /debug/flush` 立即快照；SIGTERM/SIGINT 优雅退出时最后再刷一次。

### 5. 并发

所有写入在单个 RWMutex 下串行变更（读用读锁），视图查询返回深拷贝。
并发摄入用 `go test -race` 验证。

## 目录结构

```
cmd/server/          HTTP 服务（启动恢复、定期快照、优雅退出）
cmd/synthgen/        合成流量生成器（normal / attack / truncate 三种场景）
internal/store/      基数预算核心：摄入、overflow、全局计数、快照导出/恢复
internal/persist/    JSON 快照原子读写
internal/api/        HTTP handler
examples/            请求样例 JSON + demo.sh
e2e/                 真实二进制端到端测试（含 kill -9 恢复）
docs/RUNLOG.md       实际运行命令与结果的如实记录
```

## 快速开始

```bash
go build ./...

# 用极小预算启动，方便肉眼看到 overflow
go run ./cmd/server -addr :8090 -data-dir ./data-demo -max-series 5 -flush-interval 2s

# 另一个终端
./examples/demo.sh 8090
```

或用合成流量发生器跑一次高基数攻击：

```bash
go run ./cmd/server -addr :8090 -data-dir ./data-demo -max-series 100
go run ./cmd/synthgen -addr http://127.0.0.1:8090 -mode attack -waves 50 -batch 200 -workers 8
curl -s localhost:8090/api/v1/stats | python3 -m json.tool
```

`synthgen` 三种模式：

- `normal`：只在 50 个稳定组合中取值（正常流量基线）
- `attack`：每个样本带全局唯一 `request_id`（高基数攻击）
- `truncate`：5000 字节的超长标签值（截断路径）

## HTTP API

| 方法 & 路径 | 说明 |
|---|---|
| `GET  /healthz` | 存活探针 |
| `POST /api/v1/ingest` | 批量摄入，最多 400 条/请求，body ≤ 8 MiB |
| `GET  /api/v1/stats` | 全局计数器与预算占用 |
| `GET  /api/v1/metrics` | 指标列表（不含序列明细） |
| `GET  /api/v1/metrics/{name}` | 单指标汇总；`?series=1` 含每个序列与 overflow 明细 |
| `POST /debug/flush` | 立即写快照 |

摄入请求：

```json
{
  "samples": [
    {"metric": "http_requests", "labels": {"path": "/a", "status": "200"}, "value": 12.4}
  ]
}
```

摄入响应（每条样本的判定都在 `outcomes` 里，含 `new_metric` / `new_series` / `overflow` /
`truncated` / `reason`）：

```json
{
  "received": 1, "accepted": 1, "rejected": 0, "overflow": 0,
  "outcomes": [{"accepted": true, "metric": "http_requests",
                "series_key": "4:path=2:/a;6:status=3:200;", "new_series": true}]
}
```

拒绝原因：`empty_metric`、`metric_name_too_long`、`metric_name_budget_exceeded`、
`too_many_labels`、`empty_label_key`、`label_key_too_long`。

## 配置（flag / 环境变量）

| flag | 环境变量 | 默认 |
|---|---|---|
| `-addr` | `CB_ADDR` | `:8080` |
| `-data-dir` | `CB_DATA_DIR` | `./data`（置空禁用持久化） |
| `-flush-interval` | `CB_FLUSH_INTERVAL` | `5s`（`0` 关闭定期快照） |
| `-max-series` | `CB_MAX_SERIES` | `1000` |
| `-max-metrics` | `CB_MAX_METRICS` | `1000` |
| `-max-metric-name-len` | `CB_MAX_METRIC_NAME_LEN` | `256` |
| `-max-label-keys` | `CB_MAX_LABEL_KEYS` | `20` |
| `-max-label-key-len` | `CB_MAX_LABEL_KEY_LEN` | `128` |
| `-max-label-value-len` | `CB_MAX_LABEL_VALUE_LEN` | `512` |

## 测试

```bash
go vet ./...
go test -race -count=1 ./...
```

验收测试对应关系：

| 验收项 | 测试 |
|---|---|
| 高基数攻击下内存有界 | `internal/store/attack_test.go::TestHighCardinalityAttackMemoryBounded`（5 万唯一组合，断言逻辑占用恒定、堆增长远小于攻击载荷） |
| 溢出前后计数守恒 | `TestCountConservation`、`TestExistingCombinationsNotEvicted`（以及每个测试里的守恒恒等式断言） |
| 重启恢复 | `persisttest`、`e2e::TestIngestRestartRecover`（优雅退出）、`e2e::TestHardKillRecovery`（kill -9 + 定期快照） |
| 并发摄入 | `TestConcurrentIngest`、`api_test::TestConcurrentHTTPIngestConservation`（`-race`） |

实际执行命令与原始输出见 [`docs/RUNLOG.md`](docs/RUNLOG.md)。

## 设计取舍说明

- **不实现时间窗口/过期**：本样例聚焦基数预算治理本身；快照即全部留存状态。
- **标签值截断而非拒绝**：长值通常是“意外把用户数据塞进标签”，截断后样本仍可用且不制造基数；
  截断事件计入 `truncated_label_values`。
- **单 Mutex 而非分片锁**：摄入是 map 上的 O(标签数) 操作，单锁简单、守恒推理容易；
  吞吐不是本样例的优化目标，`synthgen -workers 16` 量级没有瓶颈。
- **快照而非 WAL**：样例允许丢失最后一个 flush 周期的数据（默认 5s）；原子快照保证
  “文件要么是旧的完整状态、要么是新的完整状态”，不会读到撕裂文件。
