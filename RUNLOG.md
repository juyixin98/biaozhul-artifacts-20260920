# RUNLOG — 实际运行记录

环境：Linux 6.8.0-90-generic，Go 1.22.2 linux/amd64。以下命令与输出均为实际执行结果
（时间戳、生成的预约 ID 每次运行会不同，属正常）。

## 1. 自动化测试

```bash
$ go test ./...
?   rsrv/brute        [no test files]
?   rsrv/cmd/rsvrvd   [no test files]
ok  rsrv/sched        0.089s
ok  rsrv/server       0.010s
```

测试函数 25 个，全部通过（`go test -v` 中 25 行 `--- PASS`）：

- 差分对拍：`TestDifferentialEarliest`（4000 组随机场景/次）、`TestDifferentialFixed`
  （4000 组）、`TestDifferentialBatch`（2000 组整批 mixed fixed/earliest）
- 半开区间相邻：`TestHalfOpenAdjacency`、`TestBatchAdjacentItemsFit`
- 容量为零：`TestZeroCapacity`、`TestBatchZeroCapacityAtomic`
- 批次部分冲突 / 原子性：`TestBatchPartialConflictIsAtomic`、`TestBatchInternalConflicts`
  （失败后预约数不变、批次不落库、无 committed/reserved 事件）
- 批内求解：`TestEarliestItemPlannedSlot`、`TestEarliestNoSlot`
- 多维容量、窗口短于时长、未知/负数维度、各类校验：`TestMultiDimensionalCapacity`、
  `TestWindowShorterThanDuration`、`TestValidationErrors` 等
- 可替换时钟/执行器/事件：`TestLifecycleWithFakeClock`、
  `TestLifecyclePastIntervalActivatesAndCompletes`、`TestExecutorActivationFailure`、
  `TestEventSequencing`
- 并发：`TestConcurrentBatchesAreSerialized`（20 个相同批次并发，恰好 1 个成功）
- HTTP 端到端：`TestHTTPEndToEnd`、`TestHTTPEarliestItemBatch`

重复与竞态：

```bash
$ go test -race -count=3 ./...
?   rsrv/brute        [no test files]
?   rsrv/cmd/rsvrvd   [no test files]
ok  rsrv/sched        1.996s
ok  rsrv/server       1.066s
```

无数据竞争、重复 3 轮全部通过。开发期间还用 `-count=10` 重复最早/固定两个对拍
（每个 4000 组 × 10 轮 = 4 万起点场景/函数），通过。

覆盖率（对非平凡包）：

```bash
$ go test ./... -coverprofile=/tmp/cover.out
ok  rsrv/sched   coverage: 75.4% of statements
ok  rsrv/server  coverage: 72.4% of statements
$ go tool cover -func=/tmp/cover.out | tail -1
total: (statements) 72.0%
```

静态检查：`go vet ./...` 无输出（通过）；`gofmt -w` 已应用。

## 2. 启动服务并跑请求样例

```bash
$ go build -o /tmp/rsvrvd ./cmd/rsvrvd
$ /tmp/rsvrvd -addr 127.0.0.1:18093 -events /tmp/demo-events.jsonl
2026/... reservation scheduler listening on http://127.0.0.1:18093
$ BASE=http://127.0.0.1:18093 ./examples/demo.sh
```

各请求实际 HTTP 状态码（完整响应体见下方存档）：

| 步骤 | 请求 | 状态码 |
|---|---|---|
| 1 | 建资源 room-a（seats=10,mics=2） | **201** |
| 2 | 建零容量资源 room-full（seats=0） | **201** |
| 3 | 最早可行查询（空时间轴） | **200** → `08:00–09:30` feasible |
| 4 | 混合批次（fixed standup + earliest workshop） | **201**，workshop 紧邻落 `09:30–11:30` |
| 5 | 再次最早查询（90m, seats6/mics1） | **200** → `09:30–11:00`（在 standup 之后、与 workshop 并排） |
| 6 | 部分冲突批次 05 | **409**，仅 `overlaps-standup` 报 `capacity_exceeded`，整批拒绝 |
| 7 | 零容量批次 06 | **409**，合成冲突 `reservation_id=""`，失败维度 `seats` |
| 8 | 相邻链批次 07（09–10/10–11/11–12，满容量） | **201** 三项全部成功（相邻不冲突） |
| 9 | GET /v1/reservations | **200**，恰为 5 条（步骤 6、7 零落地） |
| 10 | GET /v1/events | **200**，11 条结构化事件 |

步骤 6 的 409 响应体（关键字段）：

```json
{"batch":{"batch_id":"batch-partial-conflict","items":[{"item_id":"overlaps-standup",
"kind":"fixed","reason":"capacity_exceeded","conflicts":[{
  "reservation_id":"r-...-1",
  "overlap":{"start":"2026-10-01T09:15:00Z","end":"2026-10-01T09:30:00Z"},
  "load_at_overlap":{"mics":2,"seats":8},"capacity":{"mics":2,"seats":10},
  "failing_dimensions":["seats","mics"]}]}]}, ... "reason":"batch_conflict"}
```

原子性核验：步骤 9 返回的预约数等于步骤 4+8 落地的 2+3=**5**；步骤 6 中单项
`fits-alone`（单独可行）随冲突项一起被回滚，没有落地。

JSONL 事件文件（`-events` 追加写出）实际内容类型序列：

```
1 resource_added          6 batch_rejected  batch-partial-conflict
2 resource_added          7 batch_rejected  batch-zero-capacity
3 batch_committed (morning) 8 batch_committed batch-adjacent-chain
4 reserved                9 reserved
5 reserved               10 reserved
                         11 reserved
```

生命周期 pump（另一次独立运行，系统时钟下预约区间已在过去）：

```bash
$ curl -s -XPOST .../v1/pump
{"at":"...","status":"pumped"}
$ curl -s .../v1/reservations   # status
["completed"]
```

完整演示输出存档于开发机 `/tmp/demo-output.txt`（共 50 行），事件 JSONL 存档于
`/tmp/demo-events.jsonl`（11 行）。

## 3. 开发过程中差分测试发现并修复的真实缺陷（如实记录）

这些缺陷均由“与离散小时间轴穷举比较”在**写测试的当下**测出，随后修复，并非事后假设：

1. **最早可行扫描的冲突区间方向写反（真 bug，对拍 iter 11 即失败）**
   初版把起点 s 与已有 `[a,b)` 的冲突范围误写成 `(b−D, a)`，正确是 `(a−D, b)`。
   症状：容量 1、已有 `[6,9)` 时，1 小时任务被错误地允许从 6 点开始（实际 6∈[6,9)
   应冲突）。穷举器给出 9 点，扫描线给 6 点。已按正确开闭关系重写推导并修复。

2. **开放窗口（无 window_end）下把临时超载当成永久（iter 23）**
   未为“进入点在窗口内、退出点在窗口外”的任务生成可见退出事件，导致 carry-in
   负载永不释放。改为以“最晚退出点”界定开放窗口扫描区域。

3. **闭窗口最晚可开始位置 `windowEnd−D` 漏检（iter 114）**
   新任务恰在该点开始、结束点与已有预约相邻时是合法解，但该点没有作为候选。
   修复方案最终被更稳健的“超载片 → 禁入开区间 → 保持触点可行的并集”算法取代
   （见 `sched/earliest.go` 注释）。

4. **事件序号重复自增**
   提交批次时先手动 `seq++`，`emitLocked` 又加一次，导致 seq 跳号（测试期望
   1,2,3 实际得到 1,2,3→第三条为 3）。改为只由 `emitLocked` 统一发号，预约 ID
   使用独立计数器。

5. **`/v1/events` 在线上返回空列表**
   服务用 `MultiSink(EventLog, JSONLSink)` 包装，而 `EventLog()` 访问器只识别裸
   `*EventLog`。改为递归穿透 MultiSink 查找内存日志。修复后实测返回 11 条事件。

6. **容量占用错误地依赖生命周期状态（批次级对拍 iter 6 发现）**
   初版把 `completed` 预约排除在容量计算外（假设“过去的预约不影响未来查询”）。
   但当用假时钟、且查询窗口位于过去时，这会漏判（穷举在 31 点，扫描线错给 28 点）。
   语义已更正为：**容量占用只取决于已提交区间，与 scheduled/active/completed/
   activation_failed 状态无关**；状态仅描述执行器侧。已删除该过滤，全量对拍通过。

另有两处为测试自身期望写错（非产品缺陷），已在测试中更正：把 `[08,10)` 与已有
`[10,12)` 的合法相邻误期望成“必须推迟到 12:00”；一个测试变量少了 `:=` 声明导致编译失败。

## 4. 未通过项 / 已知限制（如实列出）

- 最终状态下 **`go test ./...`、`go vet ./...`、`go test -race` 全部通过，无未通过项**。
- 存储为**单进程内存态**：重启即丢失（设计如此，题目要求本地接口、可替换部件）。
  `-events` JSONL 是追加审计日志，不用于回放恢复。
- 时间为连续 `time.Time`（纳秒精度），不做整小时/网格对齐约束；对拍在“整点网格”
  场景上进行，覆盖了离散验收口径，同时算法本身支持任意连续时间。
- `brute` 包是验收参考实现，刻意 O(窗口槽数 × 预约数) 的直接穷举，未做性能优化。
- 服务默认只监听 `127.0.0.1`，无鉴权、无持久化，定位为本地接口而非生产部署。
