# twopcsim —— 两阶段提交恢复：确定性离散事件模拟器

纯后端、单进程、零真实集群依赖的 **两阶段提交（2-Phase Commit, 2PC）恢复**模拟系统。
1 个协调者 + 3 个参与者全部在进程内模拟，网络可**丢包 / 重复 / 乱序**，
节点可在**任意协议阶段崩溃并从持久化 WAL 恢复**。

本项目**不回避 2PC 的固有阻塞**：当协调者在特定窗口不可用时，参与者会真实地
停留在持锁阻塞状态，报告用证据展示“阻塞”，而不是伪称系统始终可用。

- 语言：Go 1.22（仅标准库）
- 接口：JSON 场景输入 → JSON 报告输出（CLI / stdin）
- 确定性：相同输入 + 相同随机种子 ⇒ 逐事件完全可复现
- 无前端、无 HTTP 服务、无真实网络/磁盘集群（WAL 是本地文件，模拟 fsync）

---

## 1. 目录结构

```
.
├── go.mod
├── cmd/twopcsim/main.go          # JSON 运行接口（CLI）
├── internal/
│   ├── sim/scheduler.go          # 确定性离散事件调度器（虚拟时钟 + 事件堆）
│   ├── wal/wal.go                # 只追加、fsync、可重放的预写日志
│   ├── twopc/
│   │   ├── messages.go           # 协议消息（PREPARE/VOTE/GLOBAL_*/ACK/DECISION_*）
│   │   ├── context.go            # 节点接口、定时器、崩溃钩子常量
│   │   ├── coordinator.go        # 协调者状态机 + 恢复
│   │   └── participant.go        # 参与者状态机 + 终止协议 + 恢复
│   └── runner/
│       ├── scenario.go           # 场景 JSON 结构
│       ├── config.go             # 默认值与校验
│       ├── runner.go             # 组装、崩溃/重启注入、Context 实现
│       ├── network.go            # 丢包/重复/乱序网络
│       └── report.go             # 裁决、无部分提交检查、阻塞证据
├── examples/                     # 12 个请求样例（见第 6 节）
├── accept.sh                     # 一键自动化验收
└── reports/                      # accept.sh 产出的运行证据（运行后生成）
```

## 2. 快速开始

```bash
go test ./...                 # 单元 + 集成测试
go build -o bin/twopcsim ./cmd/twopcsim

./bin/twopcsim -scenario examples/01-happy-path.json
# 或从 stdin：
cat examples/01-happy-path.json | ./bin/twopcsim -scenario -
# 报告写到文件：
./bin/twopcsim -scenario examples/05-coord-down-forever-blocked.json -out report.json
```

**退出码**：`0` = 运行完成且无部分提交/协议违规（含 blocked、aborted 都算安全结果）；
`2` = 检测到部分提交或协议违规；`1` = 输入或运行错误。

一键完整验收（测试 + 12 场景 + 跨进程恢复，全部证据写入 `reports/`）：

```bash
./accept.sh
```

## 3. 协议与持久化语义

### 3.1 消息流

```
协调者                         参与者(3 个)
  │── PREPARE ──────────────────▶│  写 PART_PREPARED (fsync)，持资源锁
  │◀── VOTE_COMMIT/VOTE_ABORT ──│
  │  收齐赞成 → 写 COORD_COMMIT (fsync)   ← 提交点（不可撤销）
  │  收到否决/超时 → 写 COORD_ABORT (fsync)
  │── GLOBAL_COMMIT/ABORT ──────▶│  写 PART_COMMITTED/ABORTED (fsync)，释放锁
  │◀── ACK ─────────────────────│
```

另实现**终止协议（termination protocol）**：参与者在 PREPARED 后周期性向协调者
**和另外两个参与者**发 `DECISION_REQUEST`。协调者/已决定的同伴据其持久状态回复
`DECISION_COMMIT/DECISION_ABORT`；谁都没有决议时无人能作答。

### 3.2 WAL 记录（状态转移前必须先 fsync）

| 节点 | 记录 | 含义 |
|---|---|---|
| 协调者 | `COORD_START` | 事务开始 + 参与者名单 |
| 协调者 | `COORD_COMMIT` | **提交点**：决议为提交，此后不可回退 |
| 协调者 | `COORD_ABORT` | 决议为中止 |
| 参与者 | `PART_PREPARED` | 已就绪、投赞成票、**持锁** |
| 参与者 | `PART_COMMITTED` / `PART_ABORTED` | 终态 |

每条日志追加后立即 `Sync()`（模拟同步刷盘）。崩溃只丢失**易失状态**（内存、
待触发定时器、在途消息），已 fsync 的记录在重启后通过 WAL 重放完整恢复。

### 3.3 关键正确性规则（本模拟器强制执行）

1. **准备成功后参与者绝不自行超时回滚。** `PART_PREPARED` 之后参与者唯一的动作是
   持锁等待并周期询问决议。代码里不存在“prepared 超时 → abort”的路径
   （见 `internal/twopc/participant.go` 的 `OnTimer`，只有 `query` 定时器）。
2. **提交点不可撤销。** 协调者 fsync `COORD_COMMIT` 后，任何恢复路径都只会重发
   `GLOBAL_COMMIT`，不可能改为 abort。
3. **崩溃清掉在途消息。** 节点崩溃时，调度器撤销它拥有的定时器/入消息
   *以及它已发出但尚未到达的消息*（`scheduler.CancelNode`），模拟真实断网，
   防止“崩溃前刚发出的消息仍然送达”这种假恢复。
4. **幂等。** 重复的 PREPARE / 决议 / ACK 都被安全去重，因此网络重复与重发不会
   造成重复提交或状态机错乱。
5. **第一/第二阶段均有重发**，以对抗丢包；协调者投票超时（窗口内收不齐票）才 abort。

## 4. JSON 接口

### 4.1 请求（场景）

```jsonc
{
  "name": "string，必填",
  "seed": 1,                     // 随机种子，决定所有丢包/重复/乱序
  "maxTick": 300,                // 虚拟时钟观察窗口
  "dataDir": "var/data/run1",    // 可空（用临时目录）；resume 时复用上一次的目录
  "fresh": true,                 // true=先清空目录；跨进程 resume 设 false
  "network": {
    "lossRate": 0.0,             // [0,1] 每条消息独立丢弃概率
    "duplicateRate": 0.0,        // [0,1] 额外复制一份的概率
    "reorderRate": 0.0,          // [0,1] 附加长延迟造成乱序的概率
    "minDelay": 1, "maxDelay": 4,// 正常传播延迟区间（tick）
    "reorderDelay": 8            // 乱序消息附加延迟
  },
  "timings": { "voteTimeout": 60, "resend": 12, "query": 8 },
  "transactions": [
    { "id": "txn-A", "beginTick": 5, "voteNo": ["participant-2"] }
                                  // voteNo 可空；列出的参与者本地投否决
  ],
  "crashes": [
    {
      "nodeId": "coordinator",   // coordinator | participant-1..3
      "hook": "coordinator.commit.appended", // 协议注入点（见下表），与 atTick 二选一
      "atTick": 0,               // 或：在该 tick 直接崩溃
      "occurrence": 1,           // 钩子第几次命中时崩溃（默认 1）
      "txnId": "",               // 仅对指定事务生效（可空）
      "restartTick": 120         // >0 在此 tick 重启；0/省略=本轮永久不可用
    }
  ]
}
```

崩溃注入钩子（协议状态机中的精确位置，`before*` 副作用发生前，`*appended` 在 fsync 之后）：

| 钩子 | 位置 |
|---|---|
| `coordinator.start.appended` | START 已 fsync，未发 PREPARE |
| `coordinator.before.prepares` | 即将发 PREPARE |
| `coordinator.commit.appended` | **COMMIT 已 fsync（提交点后）**，未发 GLOBAL_COMMIT |
| `coordinator.abort.appended` | ABORT 已 fsync，未发 GLOBAL_ABORT |
| `coordinator.before.commitMsg` / `before.abortMsg` | 即将发第二阶段消息 |
| `participant.before.prepared` / `after.prepared` | 写 PREPARED 前 / fsync 后 |
| `participant.before.vote` | 即将发投票 |
| `participant.before.commit` / `after.commit` | 写 COMMITTED 前 / fsync 后 |
| `participant.before.abort` / `after.abort` | 写 ABORTED 前 / fsync 后 |

### 4.2 响应（报告，节选）

```jsonc
{
  "scenario": "...", "seed": 1, "maxTick": 300,
  "nodeSnapshots": [ /* 每节点事务状态；宕机节点由 WAL 重放重建 */ ],
  "outcomes": [{
    "txnId": "txn-A",
    "verdict": "blocked",        // committed | aborted | blocked | partial-commit
    "committed": [], "aborted": [], "prepared": ["participant-1", ...], "unknown": [],
    "partialCommit": false,      // 无部分提交的核心字段
    "blockedEvidence": {
      "coordinatorAvailable": false,
      "coordinatorDecision": "commit",   // commit | abort | unknown
      "participants": [{
        "nodeId": "participant-1", "state": "prepared", "lockHeld": true,
        "preparedTick": 8, "waitedTicks": 212,
        "lastQueryTick": 216, "queriesSent": 26
      }],
      "queryTicks": [ {"tick": 16, "from": "participant-1"}, ... ],
      "explanation": "……如实说明阻塞原因……"
    }
  }],
  "verdict": "all-consistent",   // 或 partial-commit-detected
  "protocolViolations": [],
  "crashes": [ {"nodeId":"coordinator","trigger":"hook:coordinator.commit.appended",
                "crashed":true,"restartTick":120,"restarted":true} ],
  "trace": [ {"tick":11,"node":"coordinator","kind":"coord.commit.fsynced", ...} ]
}
```

裁决规则（`internal/runner/report.go`）：
- 3 个参与者全部 committed ⇒ `committed`；无人 committed 且无人 prepared ⇒ `aborted`。
- 存在 prepared 持锁者且窗口结束仍未得到决议 ⇒ `blocked`。
- committed 与 aborted/unknown **并存** ⇒ `partial-commit`（协议被破坏，验收失败）。
  prepared 与 committed 并存不算分叉：决议为提交时，prepared 者恢复后必能收敛。

## 5. 阻塞：如实呈现而非隐藏

2PC 是**阻塞式**原子提交流议。以下场景模拟器会明确输出 `blocked` 并给出证据：

- **决议前协调者永久不可用**（`examples/06`，tick 9 直接宕机）：
  3 个参与者都已 fsync PREPARED 并投票，协调者磁盘上还没有任何决议
  （`coordinatorDecision: "unknown"`）。参与者既不能提交也不敢单方中止
  （同伴同样无决议），只能持锁、周期询问、无限等待。
- **提交点后协调者永久不可用**（`examples/05`，COMMIT fsync 后立即宕机）：
  决议已是 commit 且不可撤销，但 GLOBAL_COMMIT 从未发出。3 个参与者停在
  prepared 持锁，直到协调者恢复。

证据字段 `waitedTicks` / `queriesSent` / `lastQueryTick` / `queryTicks` 证明：
参与者在整个观察窗口内持续持锁并反复询问，且 trace 中**没有任何自行 abort 事件**。

**阻塞是可以解除的**：`examples` 的恢复场景与 `accept.sh` 第 7 步演示——用同一
`dataDir` 启动“第二个进程”（`fresh:false`），协调者重放 WAL 后重发决议，
事务从 `blocked` 收敛为 3 节点全部 `committed`。这证明阻塞是“等待权威恢复”，
不是数据损坏。

## 6. 请求样例一览（`examples/`）

| 文件 | 场景 | 预期裁决 |
|---|---|---|
| 01-happy-path | 干净网络正常提交 | committed (3/3) |
| 02-lossy-network | 25% 丢包 + 重复 + 乱序，靠重发收敛 | committed (3/3) |
| 03-coord-crash-after-start | 协调者 START 后崩溃重启 | committed |
| 04-coord-crash-after-commit | 提交点后崩溃重启，重发决议 | committed |
| 05-coord-down-forever-blocked | **提交点后协调者永久不可用 → 阻塞** | blocked |
| 06-coord-down-before-decision-blocked | **决议前协调者永久不可用 → 阻塞** | blocked |
| 07-participant-crash-prepared | 参与者 PREPARED 后崩溃，超时前恢复 | committed |
| 08-participant-crash-at-commit | 参与者 COMMITTED 后崩溃，恢复补发 ACK | committed |
| 09-vote-no-abort | 一个参与者本地否决 | aborted (3/3) |
| 10-coord-crash-after-abort | 协调者 ABORT fsync 后崩溃重启 | aborted |
| 11-multi-txn-duplicates | 两事务并发 + 35% 重复 + 乱序 | A committed / B aborted |
| 12-participant-crash-before-prepared-timeout-abort | 参与者写 PREPARED 前崩溃、投票超时 | aborted |

## 7. 确定性说明

调度器用 `(tick, 入队序号)` 最小堆推进**虚拟时钟**，没有 goroutine 时序、没有
wall clock、没有真实 socket。所有随机性（丢包/重复/乱序/抖动延迟）都来自由
`seed` 初始化的单一确定性随机源。测试 `TestDeterministic` 断言同种子两次运行的
trace 逐条一致；`TestDeterministicRng` 断言随机序列随种子复现。

## 8. 范围与非目标

- 不做前端 / 仪表盘 / HTTP 服务；接口只有 JSON-in / JSON-out。
- 不模拟真实网络分区仲裁、不实现 3PC/Paxos/Raft 等**非阻塞**协议——2PC 的阻塞
  正是本项目要显式验证的属性。
- 资源锁用布尔状态表示；不模拟具体数据项。
