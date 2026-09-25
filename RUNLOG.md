# 运行记录（RUNLOG）

环境：Linux 6.8.0-90-generic x86_64，Go 1.22.2，bash。仅用标准库，无外部服务。
以下命令与输出均为本机实际执行所得。

## 1. 构建 / 静态检查 / 测试

```
$ go version
go version go1.22.2 linux/amd64

$ gofmt -l .
（无输出：所有文件格式正确）

$ go vet ./...
vet OK

$ go build ./...
build OK

$ go test ./... -race -count=1 -cover
    tailsampling/cmd/tailsampled        coverage: 0.0% of statements
ok  tailsampling  1.099s  coverage: 73.8% of statements
```

18 个测试全部通过（含 `-race` 竞态检测）：

```
PASS: TestHashStableAndBounded
PASS: TestTokenBucketExhaustionAndRefill
PASS: TestPolicyOrdering_ErrorBeatsLatency
PASS: TestPolicyLatencyThenProbabilisticThenDrop
PASS: TestBudgetDegradation_LatencyDowngraded_ErrorStillKept
PASS: TestLateErrorSpanWithinWindowChangesOutcome
PASS: TestDecisionImmutableAndLateAfterDecisionRecorded
PASS: TestIncompleteTraceMarkedOnTTL
PASS: TestLargeTraceAggregatesCorrectly
PASS: TestForcedFlushMarksIncomplete
PASS: TestHTTPIngestAndQueryDecision
PASS: TestHTTPDecisionNotFoundAnd400s
PASS: TestHTTPStatsAndListEndpoints
PASS: TestHTTPAdminFlushFinalizesIncomplete
PASS: TestRestartReplaysDecisionsAndRearmsOpenTraces
PASS: TestLateArrivalLoggedPersistently
PASS: TestConfigValidation
PASS: TestLoadConfigFileAndEnv
```

## 2. 端到端演示：`bash scripts/demo.sh`

自动选择空闲端口启动服务，顺序执行 5 个验收场景，退出码 `0`。
关键决策（摘自实际运行 `/tmp/demo2.log`）：

**场景 1 — 错误 / 慢 / 快三类 trace：**

| trace | kept | complete | policy | reason_code |
|---|---|---|---|---|
| trace-err-001 | true | true | error | ERROR_POLICY |
| trace-slow-demo（2500ms） | true | true | latency | LATENCY_POLICY |
| trace-fast-001（3ms） | false | true | default | DEFAULT_DROP |

**场景 2 — 决策后迟到错误 span（不翻案，记录 late）：**

```json
{ "received": 1, "accepted": 0, "duplicates": 0, "late_after_decision": 1,
  "results": [{ "trace_id": "trace-fast-001", "span_id": "late-err",
                "status": "late",
                "note": "span arrived after final decision; decision is immutable" }] }
```
随后查询 trace-fast-001：`kept` 仍为 `false`，`decided_at_ms` 不变，
`late_spans` 中列出该 ERROR span。✅ 决策不可变。

**场景 3 — 预算耗尽（capacity=3, refill=0）：**

- trace-slow-002：用掉第 3 个令牌，`kept=true, budget_used=true`。
- trace-slow-003 / 004：`kept=false, degraded=true, policy=latency+budget,
  reason_code=BUDGET_DROP`，reason 明确写
  `keep budget exhausted: downgraded KEEP->DROP (degraded)`。✅ 显式降级。

**场景 4 — 大 trace（1001 span，其中 4 个 ERROR，总时长 6000ms）：**

```json
{ "received": 1001, "accepted": 1001, "duplicates": 0, "late_after_decision": 0 }
```
决策：`kept=true, policy=error, span_count=1001, error_count=4,
duration_ms=6000, degraded=true, budget_used=false`。
预算已空，错误 trace 超预算仍保留并被显式标 degraded。✅

**场景 5 — 未完整 trace（只有 child，root 永不到达）：**

- 摄入后立即查询：HTTP 404 `not finalized yet`。
- 超过 `max_ttl=3s` 后：`kept=false, complete=false, span_count=1,
  reason_code=FORCED_INCOMPLETE`，reason 写明
  `trace not observed as complete before max TTL; marked INCOMPLETE and finalized with partial data`。✅

**最终 stats（实际输出）：**

```json
{
  "total_decided": 8, "kept": 4, "dropped": 4,
  "kept_by_policy": {"error": 2, "latency": 2},
  "dropped_by_reason": {"BUDGET_DROP": 2, "DEFAULT_DROP": 1, "FORCED_INCOMPLETE": 1},
  "incomplete_decisions": 1,
  "degraded_downgrades": 2,
  "degraded_over_budget_keeps": 1,
  "late_arrivals": 1,
  "budget": {"capacity": 3, "tokens": 0, "refill_per_sec": 0, "exhausted": true}
}
```

**持久化产物（`$data_dir`）：**

```
decisions.jsonl      8 行，每行一个不可变决策
late_arrivals.jsonl  1 行（决策后迟到的 ERROR span）
snapshot.json        优雅停机后 {"taken_at_ms": ..., "open": []}
```

## 3. 请求样例：`BASE=... bash examples/curl.sh`

对真实启动的服务（`-wait-window 500ms -max-ttl 2s`）执行，退出码 `0`：
5 个 span 全部 `accepted`，1 个决策后迟到 span 标记为 `late`；
`/v1/decisions` 返回 ERROR_POLICY / LATENCY_POLICY / DEFAULT_DROP 三类决策，
`/admin/flush` 正常排空。

## 4. 开发过程中发现并修复的问题（如实记录）

这些问题由测试先暴露、随后修复，当前测试套件中均有回归用例：

1. **重启后在途 trace 被立即终结**：`scheduleLoaded` 用 `time.Until` 计算剩余
   TTL，假时钟（以及墙钟与数据时间不一致时）下差值为负，重启即入队 finalize。
   修复：统一用注入的 `Clock.Now()` 计算 `deadline.Sub(now)`。
   回归：`TestRestartReplaysDecisionsAndRearmsOpenTraces`。
2. **测试存在同步缺口**：假时钟推进后，定时器回调只是把 finalize 消息放入
   channel，测试随即读决策导致偶发“还没决策”。修复：给聚合器加 `WaitIdle`
   屏障（FIFO channel 保证此前入队消息全部处理完），并让 `SnapshotNow` 同步返回。
3. **`Config.applyEnv` 吞掉解析错误**：环境变量（如 `TS_WAIT_WINDOW=garbage`）
   解析失败时记录了 `envErr` 却没有返回。修复后返回错误。
   回归：`TestLoadConfigFileAndEnv`。
4. 其余为常规编译修正：`time.Ticker` 包装以满足 `Ticker` 接口、
   `errors.Join` 收集 Close 错误、stats 中降级计数由单一 `degraded_drops`
   拆成 `degraded_downgrades`（KEEP→DROP）与 `degraded_over_budget_keeps`
   （错误超预算保留）两个语义明确的计数。

## 5. 已知边界 / 未做项

- 决策在内存中全量保留，不做过期淘汰（样例用途；生产需 TTL/外部存储）。
- 持久化为本地 JSONL + 周期快照，每条决策 fsync；崩溃窗口内未最终化的在途
  trace 依赖快照恢复并按剩余 TTL 重新计时，不是复制日志。
- 完整性以“出现根 span（parent 为空）”判定，未做全父引用闭合校验。
- 无前端；仅 HTTP JSON。
- demo 首次在本机运行时 18080/18099 端口被其他进程占用，脚本已改为自动挑选
  空闲端口，之后运行均成功（此为环境问题，非程序缺陷）。
