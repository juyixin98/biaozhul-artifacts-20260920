# tailsampler — 尾部采样决策后端

纯后端 Go 服务：摄入 trace span，在可配置的**决策等待窗口**后按**错误 / 时长 / 预算**策略做尾部采样（tail sampling），产出每个 trace 唯一、最终、可查询的决策及原因。使用合成数据，不依赖任何真实监控平台。无前端。

## 功能与设计

- **HTTP 摄入与查询**：批量上报 span；查询单条/列表决策、保留 trace 的 span、运行统计。
- **尾部采样策略**（窗口关闭后评估，命中任一即保留）：
  - `error_span`：trace 中任一 span `status=error`；
  - `latency_exceeded(<d>ms>=<阈值>ms)`：trace 端到端时长达到阈值；
  - 未命中任何策略 → 丢弃，原因 `no_policy_matched`。
- **预算降级**：每分钟滚动窗口内保留数超过 `-budget-keeps-per-min` 时，原本命中保留策略的 trace 被**明确降级**为丢弃，决策记录 `degraded=true` 且原因含 `budget_exhausted`。
- **在途上限降级**：未决策 trace 超过 `-max-inflight-traces` 时，最老的 trace 被立即强制决策（不等窗口），记录 `degraded=true`、原因 `inflight_limit_forced_decision`。
- **决策一致性**：决策不可变，并在 `-decision-ttl`（默认 10×等待窗口）内缓存。窗口关闭后迟到的 span 命中**同一条最终决策**（摄入响应直接返回 `keep`/`drop`），不会翻案；决策持久化到 `decisions.jsonl`，重启后在 TTL 内恢复，迟到 span 仍得到原判决。
- **未完整 trace 标记**：`incomplete=true` 及 `incomplete_reasons`，来源包括
  - `late_span_after_decision`（决策后仍有 span 到达，`late_spans` 计数）；
  - `root_span_missing`（决策时未见根 span）；
  - `parent_spans_missing(n)`（存在悬空父引用）。
- **本地持久化样例**：`data/decisions.jsonl`（每条最终决策一行）与 `data/spans.jsonl`（被保留 trace 的 span，含保留后迟到的 span）。启动时重新加载决策以维持一致性；span 文件仅作留存样例，不回放。

## 构建与运行

```bash
go build -o bin/server ./cmd/server
./bin/server -addr :8080 \
  -decision-wait 10s \        # 决策等待窗口（可配置）
  -latency-threshold-ms 500 \ # 时长策略阈值
  -budget-keeps-per-min 100 \ # 每分钟保留预算（0=不限）
  -max-inflight-traces 10000 \# 在途未决策 trace 上限
  -decision-ttl 0 \           # 决策保留时长（默认 10x decision-wait）
  -data-dir data              # 持久化目录（空串=仅内存）
```

## HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/v1/spans` | 批量摄入 span：`{"spans":[{trace_id,span_id,parent_id?,service,name,start_unix_ms,duration_ms,status,attributes?}]}`，响应含每 trace 当前状态 `pending/keep/drop` |
| GET | `/v1/decisions/{traceID}` | 单条最终决策（含 reasons、degraded、incomplete、late_spans） |
| GET | `/v1/decisions?keep=&degraded=&incomplete=&limit=` | 决策列表（可按保留/降级/未完整过滤） |
| GET | `/v1/traces/{traceID}` | 被保留 trace 的全部 span（含迟到 span）；未保留返回 404 |
| GET | `/v1/stats` | 计数器：摄入/迟到 span、保留/丢弃/降级决策、预算耗尽与强制决策次数 |
| GET | `/healthz` | 健康检查 |

请求样例见 `examples/requests.sh`（`./examples/requests.sh http://localhost:8080`）。

## 合成数据发生器

`cmd/genload` 覆盖全部验收场景：

```bash
go build -o bin/genload ./cmd/genload
./bin/genload -base http://localhost:8080 -wait 12s -scenario all
# 子场景：basic | late-error | large | budget
```

## 自动化测试

```bash
go vet ./... && go test ./...
```

覆盖：错误/时长/丢弃策略、迟到 span 决策一致性（含对保留 trace 的追加存储）、缺根/缺父的未完整标记、预算耗尽降级、预算窗口滚动、在途上限强制降级、决策 TTL 淘汰、重启后一致性（JSONL 持久化恢复）、HTTP 端到端与入参校验。

## 实际运行记录（2026-09-24，本机 go1.22.2）

### 1. 单元/集成测试

```
$ go vet ./... && go test ./...
?   	tailsampler/cmd/genload	[no test files]
?   	tailsampler/cmd/server	[no test files]
ok  	tailsampler/internal/api	0.009s
ok  	tailsampler/internal/sampler	0.005s
```

全部通过，无未通过项。

### 2. 验收场景（服务参数：`-decision-wait 3s -latency-threshold-ms 500 -budget-keeps-per-min 3 -max-inflight-traces 1000 -data-dir data`）

```
$ ./bin/genload -base http://localhost:8080 -wait 5s -burst 6 -large-spans 2000 -run-id acc1 -scenario all
```

- **basic**：`acc1-normal` → `keep=false, reasons=[no_policy_matched]`；`acc1-slow`（900ms）→ `keep=true, reasons=[latency_exceeded(900ms>=500ms)]`；`acc1-error` → `keep=true, reasons=[error_span]`。
- **迟到错误 span**：`acc1-lateerr` 窗口关闭后判 `drop`；随后上报 error span，摄入响应直接返回 `"drop"`（决策未翻案），决策记录变为 `incomplete=true, incomplete_reasons=[late_span_after_decision], late_spans=1`。
- **大 trace**：`acc1-large` 共 2000 span（分 4 批 ×500 摄入）→ `keep=true, reasons=[latency_exceeded(1200ms>=500ms)], span_count=2000`。
- **预算耗尽**：窗口内此前已保留 3 条（slow/error/large，预算=3），突发 6 条错误 trace 全部被明确降级：`keep=false, reasons=[error_span, budget_exhausted], degraded=true`。stats：`traces_kept=3, traces_dropped=8, degraded_decisions=6, budget_exhausted_events=6`。

### 3. 重启一致性

```
# 摄入 restart-t1（fast/ok）→ 5s 后决策 drop；kill 服务；同 -data-dir 重启
2026/09/24 04:01:50 restored 1 of 1 persisted decisions from data2 (decision-ttl applies)
# 重启后向 restart-t1 上报迟到 error span：
{"accepted":1,"traces":{"restart-t1":"drop"}}           # 维持原判决
{"trace_id":"restart-t1","keep":false,...,"incomplete":true,"incomplete_reasons":["late_span_after_decision"],"late_spans":1}
```

### 4. 在途上限强制降级

```
# -decision-wait 30s -max-inflight-traces 2，连续摄入 3 条 trace
$ curl -s localhost:18090/v1/decisions/if-a
{"trace_id":"if-a","keep":false,"reasons":["inflight_limit_forced_decision"],"degraded":true,...}
```

### 运行中发现并处理的问题（如实记录）

1. 首次重启一致性验证失败：原决策已超出 `decision-ttl`（默认 10×3s=30s，实际间隔约 3 分钟），按设计被淘汰，迟到 span 被视为新 trace。非缺陷，但暴露了启动日志把“磁盘读到的决策数”误报为“恢复数”的问题——已修复为 `restored X of Y`，并在 TTL 内重测通过（见上）。
2. 本机 8081/18081 端口被其他服务占用，验收改用 18090；另一次 `pkill -f` 误杀自身 shell，改用 `pkill -x server`。

## 目录结构

```
cmd/server/    服务入口（flag 配置、后台决策 ticker、启动时恢复决策）
cmd/genload/   合成数据发生器（验收场景）
internal/sampler/  采样核心：策略评估、trace 缓冲、预算/上限降级、TTL、JSONL 持久化
internal/api/      HTTP 层
examples/requests.sh  curl 请求样例
```
