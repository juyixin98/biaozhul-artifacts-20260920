# 运行记录（RUNLOG）

环境：Linux 6.8.0-90-generic，Go 1.22.2 linux/amd64，无第三方依赖。
以下命令与输出均为真实执行所得。

## 1. 构建

```
$ go build ./...
$ go vet ./...
$ gofmt -l .
（无输出：全部通过、无格式问题）
```

## 2. 自动化测试

```
$ go test -race -v ./...
=== RUN   TestEmptyChannels
--- PASS: TestEmptyChannels (0.00s)
=== RUN   TestInFlightCapture
--- PASS: TestInFlightCapture (0.00s)
=== RUN   TestOverlappingSnapshotsMarkerInterleave
--- PASS: TestOverlappingSnapshotsMarkerInterleave (0.00s)
=== RUN   TestContinuousTrafficDuringSnapshots
--- PASS: TestContinuousTrafficDuringSnapshots (0.00s)
=== RUN   TestBestEffortLoss
--- PASS: TestBestEffortLoss (0.00s)
=== RUN   TestBestEffortDuplicate
--- PASS: TestBestEffortDuplicate (0.00s)
=== RUN   TestBestEffortReorderAndBrokenSnapshot
--- PASS: TestBestEffortReorderAndBrokenSnapshot (0.00s)
=== RUN   TestDeterminism
--- PASS: TestDeterminism (0.00s)
=== RUN   TestValidation（7 个子用例：无节点/重名/缺链路/金额非正/未知策略/百分比越界/marker缺id）
--- PASS: TestValidation (0.00s)
=== RUN   TestExampleRequests（6 个 examples 子用例）
--- PASS: TestExampleRequests (0.00s)
PASS
ok  	snapshotsim/internal/simulator	1.024s

$ go test -cover ./...
ok  	snapshotsim/internal/simulator	coverage: 90.4% of statements
```

无 FAIL、无 race 报告。未覆盖的 9.6% 主要是命令行 main 的文件 IO/错误退出路径，
以及 `emit` 中"重复副本又被丢弃"等低概率分支。

## 3. 样例实际运行结果

命令：`./run_examples.sh`（等价于逐个
`./bin/snapshotsim -in examples/xx.json -out examples/out/xx.result.json`）。

| 样例 | 快照 | complete | states_sum | in_flight_sum | total | 初始总量 | 守恒 |
|---|---|---|---|---|---|---|---|
| 01-empty-channels | empty | true | 150 | 0 | **150** | 150 | ✅ |
| 02-inflight-capture | inflight | true | 170 | 30（A->B 一笔 30） | **200** | 200 | ✅ |
| 03-overlap-continuous | k1 | true | 198 | 2（B->A 一笔 2） | **200** | 200 | ✅ |
| 03-overlap-continuous | k2 | true | 198 | 2（A->B 一笔 2） | **200** | 200 | ✅ |
| 04-marker-interleave | s1 | true | 300 | 0 | **300** | 300 | ✅ |
| 04-marker-interleave | s2 | true | 280 | 20（A->B 一笔 20） | **300** | 300 | ✅ |
| 05-best-effort-faults | snap | true | — | — | 199800 | 200000 | ➖ 不适用（见下） |
| 06-marker-lost-incomplete | broken | **false** | 400 | 0 | 400 | 2000 | ➖ 未完成 |

样例 05（best_effort，seed=42）信道统计：A->B 发送 24、投递 14、丢失 10、
重复 2、乱序 4；B->A 标记发送 1、投递 1（标记恰好未丢，故该次运行快照完成，
但 10 笔丢失转账使 total=199800≠200000——这是网络故障破坏守恒的预期现象）。
样例 06：A->B 100% 丢包，7 条消息（含标记）全部丢失，`complete=false`，
`missing_channels=["A->B","B->A","state:B"]`，如实报告快照无法完成。

四个 FIFO 验收场景（空信道、在途捕获、标记交错、持续流交叠快照）的
**完成快照 total 全部等于初始总量**，这是 Chandy-Lamport 正确性的直接验证。

## 4. 确定性验证

```
$ go test -run TestDeterminism -v ./...   # 同输入连跑两次，JSON 逐字节一致
--- PASS: TestDeterminism
```

## 5. 开发过程中出现过、后已修复的问题（如实记录）

1. **nil map 写入 panic**：构建链路表时内层 `map[string]*channel` 未初始化，
   `TestEmptyChannels` 首次运行即 panic。已在写入前惰性初始化。
2. **样例时序参数错误**：`TestInFlightCapture` 最初把 A->B 慢链路延迟设为 4，
   标记经 A->C->B（2 tick）与转账同 tick 到达，B 记录时余额为 80 而非预期 50，
   测试失败。这是测试用例时序推演错误（实现行为本身符合协议），将慢链路改为
   延迟 5 后标记抢先到达，用例通过。
3. **`_comment` 字段被拒**：样例 JSON 带说明字段，而 CLI 开了
   `DisallowUnknownFields`，全部样例解析失败。已在 Request 增加
   `_comment` 字段显式忽略。
4. **`missing_channels` 误为空**：不完整快照中，未记录状态的节点其入信道未被
   枚举进缺失列表；修复为对未记录节点补列全部入信道。另将空切片/null 输出
   规范化（`in_flight` 空时输出 `[]`）。

## 6. 未通过项 / 已知限制

- 最终状态：**全部测试通过，无未通过项**。
- best_effort 场景的快照不保证完成、不保证守恒——这是 Chandy-Lamport 要求
  可靠 FIFO 信道的算法前提所致，属于刻意演示而非缺陷。
- 单进程离散事件模拟，无真实网络/集群；无 HTTP 与前端（按需求不做）。
