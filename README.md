# 尾部采样决策后端（Tail Sampling Decision Backend）

纯 Go 标准库实现的可观测性数据处理后端样例：通过 HTTP 接收合成的 trace span，
按**错误 / 时长 / 采样预算**做尾部采样（tail-based sampling）决策，并提供
决策查询、原因解释与本地文件持久化。不依赖任何真实监控平台或外部中间件。

## 1. 它解决什么问题

头部采样（head sampling）在 trace 开始时就要决定，此时还不知道这个 trace 后面
会不会报错、会不会很慢。**尾部采样**等 trace 的 span 基本到齐后再决策，从而可以：

- 保留所有含错误 span 的 trace；
- 保留耗时长（尾部延迟）的 trace；
- 对其余 trace 按概率保留；
- 用一个可补充的**保留预算（token bucket）**限制保留总量。

本项目把这个过程中最容易出问题的部分都做成了明确、可查询的行为：

| 关注点 | 本项目的处理 |
|---|---|
| 等多久再决策？ | `wait_window`：trace 观测到根 span（完整）后，再等一个可配置窗口吸收迟到 span |
| trace 一直不完整怎么办？ | `max_ttl` 硬上限：超时强制决策，并明确标记 `complete=false`、原因码 `FORCED_INCOMPLETE` |
| 预算超限怎么办？ | **明确降级**：延迟/概率候选 KEEP→DROP（`degraded=true`, `BUDGET_DROP`）；错误 trace 超预算仍保留但标记 degraded |
| 同一 trace 决策一致性？ | 每个 trace 恰好决策一次，结果不可变；决策后到达的 span 记为 late，绝不翻案 |
| 为什么是这个决策？ | 每个决策带 `policy` / `reason_code` / 人读的 `reason` / 各项计数 |

## 2. 架构

```
HTTP handlers (api.go)
      │  POST /v1/spans  GET /v1/decisions/...  GET /v1/stats  POST /admin/flush
      ▼
Aggregator (aggregator.go)   —— 单 goroutine actor，串行处理所有 trace 状态
      │     ├─ open traces：按 traceID 聚合 span，等待窗口/TTL 由定时器驱动
      │     ├─ Sampler (sampler.go)：错误 → 延迟 → 概率 → 默认丢弃 的有序策略链
      │     └─ TokenBucket：保留预算（容量 + 每秒补充）
      ▼
Store (storage.go)  —— 本地文件持久化
        decisions.jsonl     决策追加日志（每行一个 Decision）
        late_arrivals.jsonl 决策后迟到 span 追加日志
        snapshot.json       在途 trace 周期快照（临时文件 + rename 原子替换）
```

关键并发设计：**所有对 trace 状态的读写只发生在 aggregator 的一个事件循环里**。
HTTP 请求、等待窗口定时器、TTL 定时器都通过 channel 投递消息，因此：

- 同一个 trace 的 span 到达、窗口到期、TTL 到期不可能并发交错；
- 一个 trace 只会被决策一次，决策后写入不可变 map；
- 定时器即使“同时”触发，也在事件循环里串行处理。

时间通过 `Clock` 接口注入，测试用手动推进的假时钟（`fakeclock.go`），
可以确定性地验证“窗口内迟到错误 span”“TTL 到期”等时序场景，不依赖 sleep。

## 3. 决策语义

策略链按顺序评估（`sampler.go`）：

1. **error**：trace 含 ≥1 个 `status=ERROR` span → KEEP（最高优先级）。
2. **latency**：trace 总时长（max end − min start）≥ `latency_threshold_ms` → KEEP 候选。
3. **probabilistic**：traceID 的 FNV 哈希落在 `probabilistic_rate` 内 → KEEP 候选。
   哈希对同一 traceID 确定，所以无论哪些 span 先到，概率分类都一致。
4. 都不满足 → 默认 DROP（不消耗预算）。

预算（令牌桶）在“候选 KEEP”阶段介入：

- 取到令牌：`kept=true, budget_used=true`。
- 令牌耗尽且是**延迟/概率**候选：显式降级 `kept=false, degraded=true`，
  `policy=latency+budget`，`reason_code=BUDGET_DROP`，原因里写明
  “downgraded KEEP->DROP (degraded)”。
- 令牌耗尽且是**错误** trace：仍然保留（丢弃错误 trace 会让采样器失去意义），
  但 `degraded=true, budget_used=false`，原因写明“kept beyond budget (degraded)”。

最终化触发方式（写进 reason）：

- `wait_window` 到期：`finalized after decision wait window`；
- `max_ttl` 到期且未观测到根 span：标记 `complete=false`，
  `reason_code=FORCED_INCOMPLETE`，并用当时的部分数据给出“临时策略”结论；
- `POST /admin/flush`：强制排空，未完整者同样标 `FORCED_INCOMPLETE`。

**不可变性**：决策一旦产生就固定。之后到达的 span：

- 不参与评估、不改变 `kept`；
- 记入 `late_spans[]` 和 `late_arrivals.jsonl`；
- 摄入响应里该 span 状态为 `late`。

注意区分两种“迟到”：

- *等待窗口内*的迟到 span（典型的迟到错误 span）——会被纳入决策（这正是要等窗口的原因）；
- *决策之后*的迟到 span——只记录，不翻案。

## 4. HTTP API

| 方法与路径 | 说明 |
|---|---|
| `POST /v1/spans` | 批量摄入 span，返回每 span 状态（accepted/duplicate/late） |
| `GET /v1/decisions/{traceID}` | 查询单个 trace 的不可变决策（含原因、迟到 span） |
| `GET /v1/decisions?limit=N&kept=true` | 列出最近决策（倒序），可按 kept 过滤 |
| `GET /v1/stats` | 计数与预算桶快照 |
| `POST /admin/flush` | 立即最终化所有在途 trace（演示/关机用） |
| `GET /healthz` | 健康检查 |

Span JSON 字段：

```json
{
  "trace_id": "trace-err-001",
  "span_id": "root",
  "parent_span_id": "",
  "name": "POST /checkout",
  "service": "checkout-api",
  "status": "ERROR",
  "error_event": "context deadline exceeded",
  "start_time_ms": 1700000000000,
  "duration_ms": 120,
  "attributes": {"http.method": "POST"}
}
```

`parent_span_id` 为空即视为根 span（用于判定 trace 完整）。时间均为 Unix 毫秒。

决策响应示例：

```json
{
  "trace_id": "trace-slow-003",
  "kept": false,
  "complete": true,
  "span_count": 1,
  "error_count": 0,
  "duration_ms": 900,
  "policy": "latency+budget",
  "reason_code": "BUDGET_DROP",
  "reason": "trace duration 900ms meets latency threshold 500ms | keep budget exhausted: downgraded KEEP->DROP (degraded) | finalized after decision wait window",
  "degraded": true,
  "budget_used": false,
  "late_spans": []
}
```

完整的可运行请求见 [`examples/curl.sh`](examples/curl.sh) 与
[`examples/*.json`](examples/)。

## 5. 配置

优先级：命令行 flag > 环境变量（`TS_*`）> JSON 配置文件 > 内置默认值。

| 配置 | flag | 环境变量 | 默认 | 含义 |
|---|---|---|---|---|
| `wait_window` | `-wait-window` | `TS_WAIT_WINDOW` | `2s` | 完整后决策等待窗口 |
| `max_ttl` | `-max-ttl` | `TS_MAX_TTL` | `10s` | 在途 trace 最大缓冲时间（≥ wait_window） |
| `latency_threshold_ms` | `-latency-threshold-ms` | `TS_LATENCY_THRESHOLD_MS` | `1000` | 尾部延迟阈值 |
| `probabilistic_rate` | `-probabilistic-rate` | `TS_PROBABILISTIC_RATE` | `0.1` | 其余 trace 基线保留率 [0,1] |
| `budget_capacity` | `-budget-capacity` | `TS_BUDGET_CAPACITY` | `100` | 保留预算桶容量 |
| `budget_refill_per_sec` | `-budget-refill-per-sec` | `TS_BUDGET_REFILL_PER_SEC` | `10` | 预算每秒补充令牌数 |
| `listen` | `-listen` | `TS_LISTEN` | `:8080` | 监听地址 |
| `data_dir` | `-data-dir` | `TS_DATA_DIR` | `./data` | JSONL/快照目录 |
| `snapshot_interval` | — | — | `5s` | 在途 trace 快照周期，0 关闭 |

示例配置：[`examples/config.json`](examples/config.json)。

## 6. 构建、运行、测试

```bash
# 构建
go build ./...

# 运行（默认 :8080，数据写到 ./data）
go run ./cmd/tailsampled

# 或用示例配置
go run ./cmd/tailsampled -config examples/config.json

# 测试（含 -race 竞态检测）
go test ./... -race -count=1

# 端到端演示（自动选空闲端口；覆盖全部验收场景）
bash scripts/demo.sh
```

无外部依赖，仅用 Go 1.22+ 标准库（模块 `tailsampling`）。

## 7. 验收场景与对应测试

| 验收要求 | 自动化测试 | 端到端演示 |
|---|---|---|
| 迟到错误 span | `TestLateErrorSpanWithinWindowChangesOutcome`（窗口内翻成 error-keep）、`TestDecisionImmutableAndLateAfterDecisionRecorded`（决策后迟到不翻案、记录 late） | demo 场景 2 |
| 大 trace | `TestLargeTraceAggregatesCorrectly`（2001 span、去重、错误计数、时长） | demo 场景 4（1001 span） |
| 预算耗尽 | `TestBudgetDegradation_LatencyDowngraded_ErrorStillKept`、`TestTokenBucketExhaustionAndRefill` | demo 场景 3 |
| 决策稳定/一致 | `TestHashStableAndBounded`（概率分类确定）、不可变测试、单 actor 设计 | demo 中重复查询结果一致 |
| 未完整 trace 标记 | `TestIncompleteTraceMarkedOnTTL`、`TestForcedFlushMarksIncomplete`、`TestRestartReplays...` | demo 场景 5 |
| 持久化/重启 | `TestRestartReplaysDecisionsAndRearmsOpenTraces`、`TestLateArrivalLoggedPersistently` | `data/` 下 JSONL/快照文件 |
| HTTP 行为 | `api_test.go`（摄入、查询、404/400、stats、flush） | `examples/curl.sh` |

实际运行命令与输出记录见 [RUNLOG.md](RUNLOG.md)。

## 8. 设计取舍与边界

- **错误 trace 超预算仍保留**：这是刻意选择——错误是尾部采样最不能丢的信号；
  代价是用 `degraded` + stats 里的 `degraded_over_budget_keeps` 显式暴露超支，
  而不是静默突破预算。若希望严格“超预算即丢”，把 `Sampler.Evaluate` 中
  `pd.priority` 分支改为降级即可（测试已覆盖两种语义的位置）。
- **完整性判定基于根 span**：`parent_span_id==""` 的 span 到达即视为完整。
  更严格可改为“span 引用的父 ID 全部在图中”，样例中保持简单可解释。
- **内存态决策全量保留**：样例不做决策过期淘汰，适合演示；生产应加 TTL/分片存储。
- **持久化是 JSONL + 周期快照**：每条决策 `write + fsync`，崩溃最多丢失快照之后
  新摄入但未最终化的 trace 的内存态（它们会在重启后按剩余 TTL 重新计时）。
  这是本地样例，不是复制日志。
- **不做前端**：仅 HTTP JSON 接口。

## 9. 源码索引

| 文件 | 内容 |
|---|---|
| `model.go` | Span / Decision / LateEvent / Stats 数据模型与原因码 |
| `config.go` | 默认配置、JSON 文件、`TS_*` 环境变量、flag 校验 |
| `sampler.go` | 令牌桶 + 有序采样策略链 + 预算降级逻辑 |
| `aggregator.go` | 单 goroutine 聚合器：等待窗口、TTL、不可变决策、计数 |
| `storage.go` | JSONL 追加日志、迟到日志、原子快照接口与文件实现 |
| `fakeclock.go` | 测试用可手动推进的假时钟 |
| `api.go` | HTTP 路由与处理 |
| `app.go`, `cmd/tailsampled/main.go` | 装配与进程入口、优雅停机 |
| `*_test.go` | 18 个自动化测试 |
| `scripts/demo.sh` | 端到端验收演示 |
| `examples/` | 配置与请求样例 |
