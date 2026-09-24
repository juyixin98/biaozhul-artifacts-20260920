# RUNLOG — 实际运行记录

- 日期：2026-09-24
- 环境：Ubuntu 24.04.4 (x86_64)，`go version go1.23.4 linux/amd64`
- 依赖：仅 Go 标准库，`go.mod` 无 `require`，因此无 `go.sum`（零外部依赖即锁定）

## 1. 静态检查与构建

```
$ gofmt -l .          # 无输出（格式干净）
$ go vet ./...        # 无告警
$ go build ./...      # 通过
```

## 2. 自动化测试

```
$ go test -v -count=1 ./...
--- PASS: TestAPI_ReplicaLifecycle
--- PASS: TestAPI_Reset
--- PASS: TestAPI_StrictJSONDecoding
--- PASS: TestAPI_ReadWriteErrorCodes
--- PASS: TestAPI_DeliverValidation
--- PASS: TestAPI_SyncValidation
--- PASS: TestClockOrderString
--- PASS: TestStore_ConcurrentConvergence          # 并发压力：8 goroutine × 25 操作后全量反熵收敛
--- PASS: TestHTTP_Acceptance_IsolatedDoubleWriteThenSync
--- PASS: TestHTTP_Acceptance_DuplicateMessage
--- PASS: TestHTTP_Acceptance_MergeThenLateStaleWrite
--- PASS: TestHTTP_Acceptance_MergeKeepsConcurrentVersion
--- PASS: TestHTTP_Acceptance_PartialMergeContext  # 部分上下文合并不误删副本已知但未声明的兄弟
--- PASS: TestHTTP_Errors
--- PASS: TestStore_IsolatedWritesThenSync
--- PASS: TestStore_DuplicateMessageDelivery
--- PASS: TestStore_ExplicitMergeThenLateStaleWrite
--- PASS: TestStore_MergeKeepsConcurrentOutsider
--- PASS: TestStore_PartialMergeContextKeepsKnownSibling
--- PASS: TestStore_PlainWriteOverwriteChain
--- PASS: TestStore_MergeMissingContextVersion
--- PASS: TestStore_SyncOneWayIsDirectional
--- PASS: TestCompareClocks (含 9 个子用例)
--- PASS: TestClockJoin
--- PASS: TestMergeVersions_CausalOverwrite
--- PASS: TestMergeVersions_ConcurrentSiblingsKept
--- PASS: TestMergeVersions_DuplicateIdempotent
--- PASS: TestMergeVersions_ConvergenceAcrossOrders # 3 条消息 6 种投递顺序，反链完全一致
--- PASS: TestMergeVersions_MergedVersionDominatesContext
ok  vcreg  0.031s
```

合计 **29 个顶层测试函数全部通过，0 失败**（含子用例共 38 个 PASS 行）。

```
$ go test -race -count=3 ./...
ok  vcreg  1.252s        # 竞态检测，重复 3 轮，全部通过

$ go test -cover -count=1 ./...
ok  vcreg  coverage: 93.8% of statements
```

未覆盖的约 6% 是 `main()` 入口和少量防御性分支（如编码后理论上不会出现的 JSON 错误）。

> 实现过程中的一次语义修正（已包含在上述测试中）：最初显式合并的时钟 join 了整个
> 副本时钟，导致 `context` 不影响结果，合并与 PUT 等价。已改为 **Dynamo 风格**：
> 合并版本仅派生自所列上下文版本，副本虽见过但未列入上下文的并发兄弟予以保留；
> 本地事件序号仍从副本级计数器分配（id 单调唯一）。
> `TestStore_PartialMergeContextKeepsKnownSibling` 与
> `TestHTTP_Acceptance_PartialMergeContext` 锁定了该行为。

## 3. HTTP 示例实际运行（examples/demo.sh）

> 环境说明：本机默认端口 18080 已被另一个无关进程占用
> （`/tmp/vcregd -addr :18080`，非本项目），因此演示实际在 **18099** 上进行：
> `/tmp/vcreg-bin -addr 127.0.0.1:18099`，`BASE=http://127.0.0.1:18099 ./examples/demo.sh`。
> 脚本退出码 `0`。以下为关键步骤的真实输出摘录。

**场景 1 — 隔离双写后同步（收敛且保留并发兄弟）**

- 隔离时：A 只见 `A-1`（clock `{A:1}`），B 只见 `B-1`（clock `{B:1}`）；
- `two-way` 同步报告：B→A 方向 `accepted: 1`，重复版本 `duplicate: 1`；
- 同步后双方都持有相同集合：

```json
[
  {"id": "A-1", "clock": {"A": 1},     "value": "from-A"},
  {"id": "B-1", "clock": {"B": 1},     "value": "from-B"}
]
```

**场景 2 — 重复消息（at-least-once 投递）**

```
第 1 次投递 A-1 -> {"A-1":"accepted"}
第 2 次投递 A-1 -> {"A-1":"duplicate"}   # 未产生第二份拷贝
```

**场景 3 — 显式合并 + 合并后迟到旧写**

- A 以上下文 `["A-1","B-1"]` 合并，产生 `A-2`，clock `{"A":2,"B":1}`，严格支配两个上下文版本，A 上只剩 `A-2`；
- one-way 同步到 B 后，B 也只剩 `A-2`；
- 延迟的合并前旧消息 `B-1`（clock `{B:1}`）到达 B：

```
{"B-1":"superseded"}        # 被判定为因果旧写，拒绝
B 的版本集合仍为 [A-2]       # 旧版本未被复活
```

**补充场景 — 合并不能误删并发版本**

- 第三方副本 C 在完全隔离状态写 `C-1`（clock `{C:1}`），从未见过 A/B；
- A 合并 A/B 后与 C 双向同步，最终 A 与 C 收敛为：

```json
[
  {"id": "A-2", "value": "resolved=A+B"},
  {"id": "C-1", "value": "from-C"}
]
```

合并版本与 C 的写并发，故两者并存——没有误删并发版本。

## 4. 复现命令

```bash
go test -race ./...
go run . -addr 127.0.0.1:18080        # 另一个终端
./examples/demo.sh                     # 端口不同时先 export BASE=http://127.0.0.1:<port>
```

## 5. 已知边界与未做的事

- **单机内存模拟**：所有副本在同一进程内，重启即丢状态；没有做持久化、真实网络传输或
  跨进程集群（题目要求"本地多副本模拟"，故刻意如此）。同步与消息投递均为显式触发，
  用于精确构造分区、乱序、重复、迟到。
- **无鉴权/TLS**：仅监听 127.0.0.1，定位为本地教学/验证工具。
- **无界面**：按要求纯后端。
- **时钟空间**：向量时钟按见过的副本累积条目，副本极多、长期运行后时钟会变大；
  未实现时钟剪枝（生产系统通常配合 full-version garbage collection），本模拟不需要。
- `DELETE /replicas/{id}` 只删除副本本身，不会清理"其他副本时钟里关于它的条目"
  （时钟只增不减，符合向量时钟语义）。
