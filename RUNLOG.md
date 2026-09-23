# RUNLOG — 实际执行记录

环境：`linux/amd64`，`go version go1.22.2 linux/amd64`。
以下所有命令与输出均在本仓库真实执行；时间为仿真虚拟毫秒。

## 1. 构建 / 格式化 / 静态检查 / 竞态

```
$ go build ./...
# 无输出，成功（产物 bin/cbsim）

$ gofmt -l .
# 无输出：所有 .go 文件已格式化

$ go vet ./...
# 无输出：干净通过

$ go test -race ./...
?  	causal-broadcast/cmd/cbsim	[no test files]
?  	causal-broadcast/internal/simnet	[no test files]
ok  	causal-broadcast/internal/engine	(cached)
ok  	causal-broadcast/internal/node	(cached)
ok  	causal-broadcast/internal/sim	(cached)
ok  	causal-broadcast/internal/vec	(cached)
```

## 2. 自动化测试（`go test -v ./...`）

全部 **PASS**，无未通过项：

| 包 | 测试 | 结果 |
|---|---|---|
| vec | TestClockObserveAndHas / TestMissingDeps / TestDepsExplicit / TestMergeAndClone | PASS |
| sim | TestSchedulerOrder / TestSchedulerChainedSameTime | PASS |
| node | TestBufferThenRelease / TestDuplicatesSuppressed / TestBackpressureDoesNotBreakOrder / TestCrossOriginCausality | PASS |
| engine | TestAcceptChainBufferRelease | PASS |
| engine | TestAcceptConcurrentReorderDuplicate | PASS |
| engine | TestAcceptLossThenRepair | PASS |
| engine | TestAcceptPermanentLossDiagnostics | PASS |
| engine | TestAcceptBackpressure | PASS |
| engine | TestBackpressureGiveUp | PASS |
| engine | TestDeterminism（两次运行逐字段 DeepEqual） | PASS |
| engine | TestExampleFilesRun（5 个 examples/*.json 全部成功且满足因果不变量） | PASS |
| engine | TestValidationErrors（7 个非法请求子用例全部返回 status=error） | PASS |

## 3. 五个验收场景的 CLI 实际结果

### 3.1 `01-chain-buffer-release.json` —— 链式 + 乱序 + 前驱补齐释放

n2 在 t=2 收到 n1:1 后触发链式广播 n2:1；n3 对 n1:1 的首跳和 n2 中继都被
延迟到 t=30。n3 在 t=30 同时拿到前驱与子消息，按因果序（n1:1 先于 n2:1）释放：

```
status: ok
  deliver t=0   node=n1 origin=n1:1 payload='chain-root'
  deliver t=2   node=n2 origin=n1:1 payload='chain-root'
  deliver t=2   node=n2 origin=n2:1 payload='chain-child'
  deliver t=6   node=n1 origin=n2:1 payload='chain-child'
  deliver t=30  node=n3 origin=n1:1 payload='chain-root'
  deliver t=30  node=n3 origin=n2:1 payload='chain-child'
  duplicates suppressed: {'n1': 1, 'n2': 1, 'n3': 2}
  root_missing_blockers: []
  undelivered_origins: []
  diag: all broadcasts delivered to all nodes in causal order
```

### 3.2 `02-concurrent-reorder-dup.json` —— 并发广播、乱序、重复

n3 **先**在 t=2 收到 n2:1，**后**在 t=20 收到 n1:1（乱序，但二者并发无依赖，
均合法投递）；n4 收到 n2:1 的重复副本，仅投递一次：

```
status: ok
  deliver t=0   node=n1 origin=n1:1 'concurrent-from-n1'
  deliver t=0   node=n2 origin=n2:1 'concurrent-from-n2'
  deliver t=2   node=n3 origin=n2:1
  deliver t=3   node=n1 origin=n2:1
  deliver t=5   node=n4 origin=n2:1
  deliver t=20  node=n3 origin=n1:1
  deliver t=24  node=n2 origin=n1:1
  deliver t=24  node=n4 origin=n1:1
  duplicates suppressed: {'n1': 1, 'n2': 1, 'n3': 2, 'n4': 3}
  root_missing_blockers: [] / undelivered_origins: []
```

### 3.3 `03-loss-repair.json` —— 丢失前驱，重传后释放

n1→n2、n1→n3 首跳全部 `drop`；t=40 由 n1 显式重传，t=42 两节点投递：

```
status: ok
  deliver t=0   node=n1 origin=n1:1 'lost-then-repaired'
  deliver t=42  node=n2 origin=n1:1 'lost-then-repaired'
  deliver t=42  node=n3 origin=n1:1 'lost-then-repaired'
  root_missing_blockers: [] / undelivered_origins: []
```

### 3.4 `04-backpressure-retry.json` —— 缓冲容量 1 + 背压 + 重试

n2 的缓冲容量=1；n1:1 被延迟到 t=40，n1:2、n1:3 先到 → 产生 **6 次背压
事件**；无限重试（间隔 5ms）在 t=40 前驱到达后成功，三条按序投递，
`retry` 最终结果仅 `delivered`：

```
  ...
  deliver t=40  node=n2 origin=n1:1 'bp-1'
  deliver t=40  node=n2 origin=n1:2 'bp-2'
  deliver t=40  node=n2 origin=n1:3 'bp-3'
  backpressure events: 6
  retry results: ['delivered']
  undelivered_origins: []
```

配套单测 `TestBackpressureGiveUp`（`max_attempts=0` 默认放弃）验证：被拒副本
记入 `retry_events[].result="given_up"`，已缓冲/已投递部分顺序仍正确。

### 3.5 `05-permanent-loss-diagnosis.json` —— 永久缺失诊断

- n1:1 对 n3 的两条路径分别被延迟 500ms（超窗）和 drop → n3 永远等不到前驱；
- n1:2 在 t=3 到达 n3，依赖 n1:1 不满足 → **滞留缓冲**；
- n1:3 对所有接收方首跳 drop（叶子拓扑无其他修复源）→ **根缺失**。

```
  deliver t=2   node=n2 origin=n1:1
  deliver t=3   node=n2 origin=n1:2
  still buffered: {'n3': ['n1:2']}
  root_missing_blockers: ['n1:3']
  undelivered_origins: ['n1:1', 'n1:2', 'n1:3']
  diag: PERMANENT LOSS: only the originating node holds these messages; no receiver
        can repair them by retransmission within end_time_ms: n1:3
  diag: NODE n3: n1:2 waits on [n1:1]
```

> 说明：`undelivered_origins` 按"至少一个节点未投递"统计（n1:1、n1:2 对 n3
> 未投递）；`root_missing_blockers` 只列**除源头外无任何节点持有**的 n1:3。

## 4. CLI 错误处理与确定性

```
$ echo '{"nodes":["a"],"end_time_ms":10}' | ./bin/cbsim ; echo exit=$?
{ "status": "error", "error": "at least 2 nodes required", ... }   # 各数组字段为 []
exit=2

$ echo 'not json' | ./bin/cbsim ; echo exit=$?
cbsim: invalid request JSON: invalid character 'o' in literal null (expecting 'u')
exit=1

$ diff <(./bin/cbsim -in examples/02-...json) <(./bin/cbsim -in examples/02-...json)
# 无输出：同 seed 两次运行逐字节相同（TestDeterminism 同时做了字段级 DeepEqual）
```

## 5. 开发过程中出现并已修复的问题（如实记录）

1. **就绪条件把消息自身当依赖**：初版 `Broadcast` 把自身 `(origin,seq)` 加进
   依赖向量，导致每条消息"永远等待自己"、全部滞留缓冲。改为标准规则
   （非自身依赖 + 同源 `seq-1` 前驱）后修复，并有 `TestBufferThenRelease` 锁定。
2. **调度器窗口边界**：初版 `Run` 用 `Time < endTime`，同虚拟时刻链式事件被
   提前截断。改为 `Time <= endTime` + 哨兵置于 `endTime+1`，
   `TestSchedulerChainedSameTime` 覆盖。
3. **故障动作串不匹配**：JSON 用 `"drop"`/`"duplicate"`，switch 一度只匹配
   内部常量 `"dropped"`，导致丢包故障被静默放行。已对齐并加校验
   （`TestValidationErrors/invalid_fault_action`）。
4. **全互联 gossip 掩盖丢包**：首跳 drop 后会被其他接收方中继自然修复，无法
   演示"真正丢失"。引入有向 `topology`，并约定省略 `from` 的故障只锚定源头
   直发副本。
5. 若干测试预期曾按"单一路径"书写，未考虑中继旁路；已改为与实际多路径语义
   一致的断言。

当前 `go test ./...`、`go test -race ./...`、`go vet ./...`、`gofmt -l .`
均无未通过项。完整的机读结果见 `examples/out/*.out.json`。
