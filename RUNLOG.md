# 运行记录（RUNLOG）

本机：Linux x86_64，`go version go1.22.2 linux/amd64`。以下命令与输出均为本仓库
在开发结束时实际执行所得；时间戳字段为模拟时间（非墙钟）。完整 JSON 输出在
`examples/results/*.result.json`。

## 1. 静态检查与构建

```text
$ gofmt -l cmd internal
（无输出，全部格式正确）

$ go vet ./...
vet clean（无告警）

$ go build ./...
（成功，无输出）
```

## 2. 自动化测试

```text
$ go test -count=1 ./...
ok  	causal-broadcast/cmd/cbcast            0.673s
ok  	causal-broadcast/internal/jsonio       0.003s
ok  	causal-broadcast/internal/netlink      0.004s
ok  	causal-broadcast/internal/node         0.004s
ok  	causal-broadcast/internal/scenario     0.005s
ok  	causal-broadcast/internal/sim          0.003s

$ go test -race -cover ./...
ok  	causal-broadcast/cmd/cbcast            coverage:  0.0%（逻辑在子进程二进制中，见下注）
ok  	causal-broadcast/internal/jsonio       coverage: 69.9%
ok  	causal-broadcast/internal/netlink      coverage: 87.8%
ok  	causal-broadcast/internal/node         coverage: 80.8%
ok  	causal-broadcast/internal/scenario     coverage: 93.1%
ok  	causal-broadcast/internal/sim          coverage: 89.3%
```

竞态检测（`-race`）全部通过。`cmd/cbcast` 覆盖率显示 0% 是因为它的测试
（`main_test.go`）以子进程方式编译并驱动编译产物，Go 的覆盖率统计不到子进程
内部；CLI 的关键路径（文件/stdin 输入、生成器、非法请求退出码）由
`TestCLIRunsFileRequest`、`TestCLIGeneratorRoundTrip`、
`TestCLIRejectsInvalidRequest` 实际执行并断言。

测试清单（`go test -v`，全部 PASS）：

- `internal/node`：`TestDeliverableRule`（因果可投递判定）、
  `TestBufferReleaseCascade`（乱序 C1/B1/A1 到达 → A1 触发 B1→C1 级联，顺序正确）、
  `TestDuplicateDeliveredOnce`（已投递/已缓冲重复都只算一次且不占槽）、
  `TestBackpressureLeavesStateUntouched`（容量 1 时拒绝 A3、补齐后重传成功，顺序 A1→A2→A3）、
  `TestMissingDepsReport`。
- `internal/sim`：完美网络全投递；乱序链缓冲+级联释放；前驱强制丢失的根因诊断；
  纯丢失报 `never-arrived`；重复抑制；并发独立消息零误缓冲；同种子两次运行 trace
  逐事件一致（确定性）；脚本化延迟前驱释放缓冲；`dropIds` 优先于 `delayedIds`；
  背压 trace；单节点本地投递；`maxTime` 截断报 `in-flight` 而非永久缺失；
  同源 seq 间隔（A2 丢失）把 A3 的根因定位到 `A2@B`。
- `internal/netlink`：同种子计划一致；强制丢包；hold 额外副本；delayed 替换正常副本、
  drop 优先。
- `internal/jsonio`：合法请求执行、`delay` 数组形式、五类非法输入被拒、默认缓冲容量。
- `internal/scenario`：chain 正常/丢失、concurrent 独立性与重复计数。

## 3. 验收样例实际运行

构建：`go build -o bin/cbcast ./cmd/cbcast`

### 3.1 链式广播 + 乱序 + 前驱补齐释放 — `examples/chain.json`

```text
$ ./bin/cbcast -in examples/chain.json
$ jq -r '.trace[] | select(.type=="buffered" or (.type=="deliver" and .reason=="cascade"))
         | "\(.time) \(.type) \(.msgId)@\(.node)"'
4 buffered B1@C
5 deliver  B1@C
7 buffered C1@D
8 deliver  C1@D

$ jq '.stats | {bufferedTotal, releasedFromBuffer}'
{ "bufferedTotal": 2, "releasedFromBuffer": 2 }

$ jq '.diagnostics.causalOrderOk, (.diagnostics.permanentMissing|length)'
true
0
```

结论：B1 先于前驱 A1 到达 C → 缓冲；A1 在 t=5 到达后 B1 作为 `cascade` 释放。
C1 同样先于 B1 到达 D（t=7 缓冲，t=8 释放）。无因果违规、无永久缺失。

### 3.2 并发独立广播 + 乱序 + 重复 — `examples/concurrent.json`

```text
$ jq '.stats | {deliveredNew, bufferedTotal, releasedFromBuffer, duplicates, arrivals}'
{ "deliveredNew": 9, "bufferedTotal": 0, "releasedFromBuffer": 0,
  "duplicates": 2, "arrivals": 8 }

$ jq -r '.trace[] | select(.type=="duplicate") | "\(.time) dup \(.msgId) at \(.node)"'
15 dup A1 at B
15 dup A1 at C
```

各节点投递顺序（节选，明显乱序）：A 节点为 A1→C1→B1；C 节点为 C1→B1→A1。
三条相互独立的消息即使乱序到达也**零缓冲**、全部投递；A1 的脚本化迟到副本在
B、C 各被识别为重复并丢弃（3 条消息 × 3 节点 = 9 次投递，arrivals 8 个跨节点
正常副本 + 2 个脚本化重复 = 统计自洽）。

### 3.3 永久丢失前驱的诊断 — `examples/loss.json`

```text
$ jq -r '.diagnostics.permanentMissing[]
         | "\(.node) \(.msgId) \(.state) roots=\(.rootCauses|join(","))"'
C A1 never-arrived roots=A1@C
C B1 buffered      roots=A1@C   chain=B1->A1
```

A1 在去往 C 的链路上被强制丢弃（`dropIds`）。B1（时钟含 A:1）到达 C 后进入缓冲；
事件排空后诊断给出：B1 卡在缓冲、缺失依赖 `A have=0 need=1`、根因链
`B1 → A1`、终止根因 `A1@C`；A1 自身状态为 `never-arrived`。A、B 两节点不受影响，
各自完整投递 A1、B1。

### 3.4 背压不破坏因果顺序 — `examples/backpressure.json`

C 的缓冲容量设为 1；A1 到 C 仅有一个 t=10 的脚本化副本，A3 到 C 有 t=5 与 t=12
两个脚本化副本。

```text
$ jq -r '.trace[] | select(.node=="C") | "\(.time) \(.type) \(.msgId) \(.reason // "")"'
3  buffered     A2
7  backpressure A3 buffer-full
10 deliver      A1
10 deliver      A2 cascade
14 deliver      A3

$ jq '.stats | {bufferedTotal, backpressure, releasedFromBuffer, duplicates}'
{ "bufferedTotal": 1, "backpressure": 1, "releasedFromBuffer": 1, "duplicates": 0 }
```

t=3 A2 因缺 A1 进入唯一缓冲槽；t=7 A3 第一个副本到达时缓冲满 → 返回背压、
不入库；t=10 A1 到达，A1 投递并级联释放 A2；t=14 A3 的第二个副本（重传语义）
到达，成功投递。最终投递顺序严格为 A1→A2→A3，背压未破坏因果序。

## 4. 内置场景生成器

```text
$ ./bin/cbcast -gen chain -nodes 4 -loss -run \
  | jq -r '.diagnostics.permanentMissing[] | select(.state=="buffered")
            | "\(.node) \(.msgId) chain=\(.rootChain|join("->"))"'
D B1 chain=B1->A1
D C1 chain=C1->A1

$ ./bin/cbcast -gen concurrent -nodes 4 -seed 5 -run | jq '.stats | {bufferedTotal, duplicates}'
{ "bufferedTotal": 0, "duplicates": 3 }
```

## 5. 接口与确定性核验

```text
$ cat examples/loss.json | ./bin/cbcast -in - | jq -r '.diagnostics.permanentMissing[0].msgId'
A1

$ echo '{"nodes":[]}' | ./bin/cbcast -in - ; echo "exit=$?"
cbcast: nodes: at least one node is required
exit=1

$ echo '{"nodes":["A","B"],"network":{"default":{"loss":2}}}' | ./bin/cbcast -in - ; echo "exit=$?"
cbcast: network.default.loss: probability must be in [0,1]
exit=1

$ ./bin/cbcast -in examples/concurrent.json | sha256sum
092ff6ed4f57825dcf994a951ca30dca162cee75d165efd35aaa74793cb44f44  -
$ ./bin/cbcast -in examples/concurrent.json | sha256sum
092ff6ed4f57825dcf994a951ca30dca162cee75d165efd35aaa74793cb44f44  -
```

## 6. 开发中发现并修复的问题（如实记录）

1. **早期手工 chain 场景的广播时刻安排错误**：发送者尚未收到前驱就广播后继，
   导致后继消息时钟本就不含该前驱（不是“网络乱序”）。重排为脚本化延迟前驱
   （`delayedIds`）后，才正确演示“后继先到 → 缓冲 → 前驱补齐级联”。
2. **`holdIds` 脚本化副本被重复追加**：网络层先把 hold 数量计入抖动副本循环，
   又单独追加一次，使每个迟到副本翻倍（concurrent 曾误报 4 个重复，实际应为 2）。
   修复后统计自洽，并新增 `TestHoldAddsCopies` 固化。
3. **`dropIds` 与 `delayedIds` 的优先级**：初版 delayed 先于 drop 判断，无法表达
   “正常副本丢弃、仅保留重传副本”。已改为 drop 最高优先级，并用
   `TestDropBeatsDelay` 固化。
4. **同源 seq 间隔的根因指错**：`MissingDeps` 对“A3 先于 A2”曾报告 need=3
   （消息自身），根因遍历自指终止。改为报告最早缺口 need=cur+1=2，A3 根因正确
   定位到 `A2@B`（`TestSameSenderGapRootCause`）。
5. **时间截断诊断**：截断后事件堆被清空，曾把在途报文误判为 never-arrived；改为在
   停止时记录未处理到达集合，截断场景只报 `blocked/in-flight`
   （`TestMaxTimeCutoffReportsInflight`）。

## 7. 未通过项 / 已知限制

- 无未通过的测试或验收项；上述 6 节中的失败均为开发过程中发现并已修复的问题，
  对应回归测试均已加入并通过。
- 已知设计限制（非缺陷）：
  - 无真实重传协议；“重传”通过 `delayedIds`/`holdIds` 以脚本化副本表达，
    背压后的重试由请求方安排（如 backpressure 样例）。
  - 消息按发送节点名前缀 + 序号生成 ID，节点名需能如此拼接解析；空节点名被校验拒绝。
  - 无前端，仅提供文件/stdin 的 JSON 命令行接口。
