# 运行记录（RUNLOG）

- 日期：2026-09-24
- 环境：Linux 6.8.0-90-generic x86_64，Go 1.22.2，无第三方依赖（标准库 only）
- 所有命令均在项目根目录实际执行，以下为真实输出（路径/端口以当次运行为准）。

## 1. 构建与静态检查

```
$ gofmt -l .
（无输出：0 个未格式化文件）

$ go vet ./...
（无输出：通过）

$ go build ./...
（无输出：通过）
```

## 2. 单元/集成测试

```
$ go test ./... -count=1
ok  	cardinalgov/internal/governor	1.875s
ok  	cardinalgov/internal/httpapi	0.798s
?   	cardinalgov/cmd/server	[no test files]
?   	cardinalgov/cmd/synthload	[no test files]
```

20 个测试全部 PASS：

- governor（13）：TestSeriesBudgetAndOverflow、TestPerMetricBudgetOverride、
  TestMetricNameBudget、TestLabelValidation、TestTruncateUTF8、
  TestIngestCopiesLabels、TestConcurrentIngest、
  **TestHighCardinalityAttackMemoryBounded**、TestSnapshotRestoreConservation、
  TestRestoreAppendsSurvive、TestSaveSnapshotAtomic、TestRestoreFoldWhenBudgetShrunk、
  TestRestoreFailsWhenMetricNameLimitShrunk
- httpapi（7）：TestIngestSingleAndBatch、TestIngestRejectsBadJSON、
  TestQueryAndStats、TestConcurrentHTTPIngest、TestSnapshotEndpoint、
  TestSnapshotEndpointDisabled、TestHealth

竞态检测：

```
$ go test -race -count=1 ./...
ok  	cardinalgov/internal/governor	9.146s
ok  	cardinalgov/internal/httpapi	2.761s
```

## 3. 高基数攻击夹具（内存有界验收）

```
$ go test ./internal/governor/ -run TestHighCardinalityAttackMemoryBounded -v -count=1
attack_test.go:99: HeapAlloc before=293KiB afterWave1=268KiB afterWave2=269KiB
  grow1=-25KiB grow2=1KiB total=-23KiB (attacked 500000 unique series)
--- PASS: TestHighCardinalityAttackMemoryBounded (1.75s)
```

解读（budget=64）：

- 先建 64 个稳定组合（各 3 样本），随后灌入 **500,000 个全局唯一标签组合**（两波各 25 万）。
- 每波攻击后强制 `runtime.GC` 读 `HeapAlloc`：第二波 25 万新组合只带来约 **1 KiB**
  堆变化（断言上限 8 MiB/波、16 MiB/总计，远低于阈值）。
- tracked 组合数全程恒为 64；攻击后稳定组合仍各为 3 样本，再写仍走 tracked 路径。
- 计数：received = 64×3 + 500000 + 1 = 500193；accepted(tracked) = 193；
  overflowed = 500000；rejected = 0；series_created = 64；series_evicted = 0。
- 对照含义：若没有预算治理，50 万个唯一组合会新建 50 万个 map 项
  （数百 MiB 堆增长），测试即会失败。

> 说明：Go 的堆指标受 GC 调度影响，绝对值有噪声，因此夹具用“第二波增量”
> 而非绝对值判据；多次运行增量均在数 KiB 量级。

## 4. 端到端演示（真实 HTTP 服务 + 重启恢复）

命令：`bash scripts/demo.sh`（动态选取空闲端口，临时目录工作，退出自动清理进程）。

一次通过的运行关键输出：

攻击后统计（20 万唯一 `request_id`，预算 50）：

```json
{
  "counters": {
    "received": 200154,
    "accepted": 154,
    "overflowed": 200000,
    "rejected": 0,
    "series_created": 54,
    "series_evicted": 0,
    "values_truncated": 0
  },
  "metric_names": 3,
  "tracked_series": 54,
  "overflow_series": 1,
  "conservation_ok": true
}
```

攻击指标视图（脚本内断言 tracked==50 且 overflow==200000，通过）：

```
series_budget: 50
tracked_series: 50
overflow: {'labels': {'__bucket__': 'overflow'}, 'value_sum': 200000,
           'sample_count': 200000, 'last_updated': 1790193142493}
```

守恒核对：154 + 200000 + 0 = 200154 = received ✓。
（154 = 4 个示例组合 + 50 个攻击稳定组合；200000 攻击样本全部 overflow；
另有示例摄入的 100 个 tracked 重复命中不计入 created。）

边缘批次（HTTP 202，含 1 次值截断、2 条拒绝）：

```json
{"received": 6, "accepted": 4, "rejected": 2, "overflowed": 0,
 "errors": [
   {"index": 4, "reason": "invalid_metric_name"},
   {"index": 5, "reason": "invalid_label_key"}]}
```

落盘、SIGTERM 退出、从快照重启：

```
final snapshot saved to /tmp/tmp.xxxx/state.json
restored snapshot: metrics=3 tracked_series=58 received=200160 overflowed=200000 rejected=2
```

重启后 `/stats` 与停机前逐字段一致（`conservation_ok: true`）；重启后再发
一个新唯一组合，正确累加进恢复出来的 overflow 桶（响应 `overflowed: true`）。

## 5. 过程中遇到并修正的问题（如实记录）

1. **演示脚本首次运行失败**：固定端口 18080 被本机无关进程（`vccsim`）占用，
   server 报 `bind: address already in use`，脚本却继续向占用者发请求得到 404。
   修正：脚本改为启动时动态申请空闲端口（Python 绑定端口 0）。重跑通过。
2. **演示计数叙事不干净（第一次实现）**：示例样本与攻击共用指标
   `http_requests`，先占用了 3 个预算槽，导致 50 个稳定组合只建成 47 个、
   overflow=200009。功能正确但数字不直观。修正：攻击改用独立指标
   `attack_requests`，得到 tracked=50、overflow=200000 的清晰结果。
3. 开发中的编译期问题（`Counters` 含 map 不可直接 `!=` 比较、StatsView 字段
   路径笔误）均在首次 `go vet`/`go build` 时发现并修复，未遗留到测试阶段。
4. 自查时发现恢复路径里 `foldMetric` 想把“超过全局指标名预算”的指标静默折叠进
   一个合成指标，但该路径没有测试且会改变查询语义。改为显式报错
   （`TestRestoreFailsWhenMetricNameLimitShrunk` 覆盖），并删除死代码。
   期间两次因记错默认值（MaxMetricNames 实际默认 512）导致字符串替换夹具未命中，
   均通过实际序列化输出核对后修正——记录在此以说明最终数字来自真实运行而非记忆。

## 6. 未通过项 / 已知限制

- 无未通过测试：20/20 测试 PASS，`-race` 无告警，demo 退出码 0。
- 已知限制（设计取舍，非缺陷）：
  - 持久化为整库 JSON 快照（周期 + 退出时），非 WAL；超大状态下落盘有放大。
  - overflow 为单一粗粒度桶，不保留被聚合组合的维度细节。
  - 无鉴权/TLS、无 TTL、无分片、无前端（按需求刻意不做）。
