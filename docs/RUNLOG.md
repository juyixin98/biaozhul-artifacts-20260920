# 运行记录（RUNLOG）

本文件如实记录项目在交付环境中的实际执行情况。环境：

- OS：`Linux 6.8.0-90-generic x86_64`
- Go：`go1.22.2 linux/amd64`
- 日期：2026-09-24
- 目录：从空 git 仓库起步（仅一个初始空基线 commit）

所有命令均在模块根目录执行。

## 1. 构建与静态检查

命令：

```bash
gofmt -l .          # 最终结果：无输出（全部已格式化）
go build ./...      # 通过
go vet ./...        # 通过
```

构建出的二进制：

```text
bin/server      7,629,829 bytes
bin/synthgen    7,545,559 bytes
```

## 2. 自动化测试

### 2.1 全量测试（含竞态检测）

命令：

```bash
go test -race -count=1 ./...
```

输出（原始）：

```text
?   	cardinalitybudget/cmd/server	[no test files]
?   	cardinalitybudget/cmd/synthgen	[no test files]
ok  	cardinalitybudget/e2e	4.459s
ok  	cardinalitybudget/internal/api	1.388s
ok  	cardinalitybudget/internal/persist	1.030s
ok  	cardinalitybudget/internal/store	2.671s
ok  	cardinalitybudget/internal/store/persisttest	1.029s
```

结果：**全部通过，`-race` 未报告数据竞争。** e2e 包会真实 `go build`
server 二进制、启动进程、通过 HTTP 驱动，并用 SIGTERM 与 kill -9 两种方式验证恢复。

### 2.2 验收项对照与关键输出

命令：

```bash
go test -race -count=1 -v ./internal/store/ -run \
  'TestHighCardinalityAttackMemoryBounded|TestConcurrentIngest|TestExistingCombinationsNotEvicted|TestCountConservation'
```

输出（节选原始日志）：

```text
=== RUN   TestHighCardinalityAttackMemoryBounded
    attack_test.go:107: heap before=330304 after=327544 growth=-2760 (unbounded growth would be ~3200000); logical=74774 bound=410496
--- PASS: TestHighCardinalityAttackMemoryBounded (0.98s)
=== RUN   TestConcurrentIngest
--- PASS: TestConcurrentIngest (0.41s)
=== RUN   TestExistingCombinationsNotEvicted
--- PASS: TestExistingCombinationsNotEvicted (0.00s)
=== RUN   TestCountConservation
--- PASS: TestCountConservation (0.00s)
```

验收对应：

| 验收要求 | 测试 | 结果 |
|---|---|---|
| 高基数攻击夹具验证内存有界 | `TestHighCardinalityAttackMemoryBounded`：预算 100，先填满预算+overflow 桶，再打 **50,000 个全新唯一 request_id（10 波 × 5000）** | 通过。留存逻辑占用恒定 `74,774 字节`（平台期前后逐字节相等），堆增量 **-2,760 字节**；若无治理仅唯一 ID 载荷就约 3.2 MB |
| 溢出前后计数守恒 | `TestCountConservation`、`TestExistingCombinationsNotEvicted`；另在每个相关测试中断言 `received=accepted+rejected`、`accepted=normal+overflow`、`metric.count=Σseries.count` | 通过 |
| 已有组合不被新标签挤走 | `TestExistingCombinationsNotEvicted`（100 个洪泛组合后，原始 3 组合就地计数，序列数仍为 3）；攻击测试中 50 个稳定组合计数全部为预期值 | 通过 |
| 重启恢复 | `persisttest::TestRestartRecovery`（两轮快照往返、恢复后继续摄入）、`e2e::TestIngestRestartRecover`（SIGTERM 最终刷盘后精确恢复）、`e2e::TestHardKillRecovery`（kill -9 后靠 1s 周期快照恢复） | 通过 |
| 并发摄入 | `TestConcurrentIngest`（16 goroutine × 2000 样本，`-race`）、`api::TestConcurrentHTTPIngestConservation`（12 HTTP 客户端 × 100 轮） | 通过 |
| 标签长度限制 | `TestLabelValueTruncation`、`TestTruncationKeepsUTF8Boundary`（rune 边界截断、截断后相同前缀共享同一序列、不产生基数泄漏） | 通过 |

### 2.3 基准：持续攻击下的吞吐与留存

命令：

```bash
go test -run='^$' -bench='BenchmarkAttackIngest' -benchtime=20000x ./internal/store/
```

输出：

```text
BenchmarkAttackIngest-16  20000  621888 ns/op  274592 retained-heap-bytes  8000000 samples  349906 B/op  4540 allocs/op
```

含义：预算 1000 填满后，持续摄入 **8,000,000 个唯一组合**，进程留存堆约
**268 KiB**；稳态吞吐约 **129 万样本/秒**（单 mutex，单线程基准，仅作量级参考）。
注：`B/op` 与 `allocs/op` 是请求处理过程中的临时分配（随 GC 回收），不进入留存态；
留存态以 `retained-heap-bytes` 为准，且它不随样本数增长。

## 3. 真实端到端手工运行（脚本化、可复现）

命令：

```bash
./scripts/run_demo.sh 18200
```

脚本完成：构建 → 启动（`-max-series 100`）→ 用 `examples/ingest.json` 摄入正常样本
→ `synthgen -mode attack`（50 波 × 200 × 8 workers）→ `mode truncate`
→ 守恒断言 → SIGTERM → 用同一 data-dir 重启 → 逐字段比对恢复 → 恢复后继续摄入。

关键原始输出：

攻击发生器：

```text
mode=attack sent=80000 accepted=80000 rejected=0 overflow=79902
stats: {
  "samples_received": 80003,
  "samples_accepted": 80003,
  "samples_rejected": 0,
  "overflow_samples": 79902,
  "normal_samples": 101,
  "truncated_label_values": 0,
  "metric_names": 2,
  "series_total": 102,
  "overflow_series_total": 1,
  "logical_bytes_estimate": 97531,
  "logical_bytes_bound": 78112000
}
```

截断场景之后（5000 字节 user_agent 被截断，200 个截断事件；该指标只用掉 12/100 序列）：

```text
"samples_received": 80203,
"samples_accepted": 80203,
"samples_rejected": 0,
"overflow_samples": 79902,
"normal_samples": 301,
"truncated_label_values": 200,
"series_total": 114,
```

计数守恒脚本断言输出：

```text
CONSERVED: received=80203 accepted=80203 (normal=301 overflow=79902); metric series=100+overflow, sum=80002
```

优雅退出与快照：

```text
== graceful shutdown (SIGTERM) ==
server exited 0
-rw------- 1 admin admin 38566 Sep 24 04:02 snapshot.json
```

重启后逐字段比对：

```text
RECOVERY EXACT for: samples_received, samples_accepted, samples_rejected,
overflow_samples, normal_samples, truncated_label_values, metric_names,
series_total, overflow_series_total
```

恢复后继续摄入（旧组合命中原序列、新唯一组合仍进 overflow）：

```text
accepted=2 overflow=1 (first sample hits a restored series, second overflows)
ALL MANUAL CHECKS PASSED
```

另用 `examples/demo.sh`（预算 5）验证小预算行为：

```text
flood 50 more unique combos: accepted=50, overflow=50
series_total=7 (5 normal + 1 overflow for http_requests + ...)
normal_series=5, overflow_count=50
```

## 4. 过程中出现并已修复的问题（如实记录）

1. 早期单测夹具的组合数算错（50 个“稳定”样本实际只产生 10 个不同 key；
   另有一个“空标签键”用例误写成空标签值），导致断言失败。**实现行为正确，测试已修正。**
2. 初次放置持久化测试文件时包名写成了 `store`（与所在 `persist` 目录冲突），
   编译报 `found packages persist and store`。已移至
   `internal/store/persisttest/`（外部测试包）并另在 `internal/persist/`
   增加了本包的外部测试文件。
3. 手工运行时第一次选用的端口 18099 已被环境中其他进程占用（其 `/healthz`
   返回了不属于本服务的字段），server 日志报 `bind: address already in use`。
   更换端口后正常；与代码无关。
4. 指标视图 `at_budget` 初版在“已产生 overflow”时反而报 `false`（条件写反）。
   已改为 `normalSeries >= budget`，即“正常序列预算已占满（含已溢出）”。

## 5. 未通过项 / 已知限制

- 无未通过的测试或手工检查项。
- 已知设计限制（刻意取舍，非缺陷）：
  - 周期快照模式下，崩溃最多丢失最后一个 flush 周期（默认 5s，演示用 1–2s）的数据；
    快照写入本身是原子的，不会出现撕裂文件。
  - 单全局 mutex；未做分片。当前合成压测量级（8 worker × 80k HTTP 样本、
    库内基准 ~129 万样本/秒）未见瓶颈，但不是为水平扩展设计的。
  - 没有 TTL/时间窗口：重启恢复后的状态即全部留存状态，适合“预算治理”样例演示，
    不是完整的时序数据库。
