# 两阶段提交（2PC）崩溃恢复 —— 确定性离散事件模拟器

纯后端项目。在**单进程内**模拟 1 个协调者（coordinator）+ 3 个参与者（participant，p1/p2/p3），
使用确定性离散事件仿真（virtual clock + 事件队列），网络可配置**丢包、重复、乱序**，
支持在任意协议里程碑精确注入崩溃与重启。**不依赖任何真实集群、网络、goroutine 或系统时钟**；
相同输入（含随机种子）必然产生逐字节一致的执行轨迹。

本项目如实建模**阻塞式 2PC**：参与者一旦把 `prepared` 持久化，就**不允许自行超时回滚**；
当协调者不可用时，相关事务会**阻塞**——这是 2PC 的本质属性，本项目用专门的场景和量化证据
展示阻塞，而不是伪装成"始终可用"。

---

## 1. 目录结构

```
.
├── go.mod
├── main.go                     # CLI：读取 JSON 场景 -> 运行 -> 输出 JSON 结果 + 摘要
├── internal/
│   ├── engine/                 # 离散事件内核：虚拟时钟、事件堆、不可靠网络、崩溃/重启
│   ├── wal/                    # 预写日志：每条记录 Append 后 fsync，崩溃后重放恢复
│   ├── twopc/                  # 协调者 / 参与者状态机 + 协议消息
│   └── harness/                # JSON 场景装配、崩溃注入、结果判定与安全不变量检查
├── scenarios/                  # 10 个 JSON 请求样例（也是验收用例）
└── examples/output/            # 部分场景的真实运行输出（完整事件轨迹证据）
```

## 2. 构建与运行

要求 Go 1.22+，无第三方依赖。

```bash
go build ./...
go run . scenarios/01-happy-commit.json            # 完整 JSON 打到 stdout，摘要打到 stderr
go run . --quiet scenarios/08-blocking-coord-down.json   # 只看摘要
go run . --out result.json scenarios/07-coord-crash-after-decision.json
go test ./... -count=1
```

## 3. JSON 请求格式（场景文件）

```json
{
  "seed": 1,
  "horizon": 120,
  "network": { "baseDelay": 1, "jitter": 1, "loss": 0.0, "duplicate": 0.0 },
  "dataDir": "",
  "requests": [
    { "txnId": "T1", "at": 10, "writes": [ { "key": "balance", "value": "100" } ] }
  ],
  "crashes": [
    { "node": "coord", "milestone": "c-decision-wal:T1", "downTicks": 40 }
  ],
  "initialKv": { "p1": { "x": "1" } }
}
```

| 字段 | 含义 |
|---|---|
| `seed` | 随机种子，决定丢包/重复/延迟抖动；同种子结果确定 |
| `horizon` | 虚拟时钟上界（ticks），事件队列排干或超过该值即停 |
| `network.baseDelay` / `jitter` | 单向延迟 = baseDelay + uniform[0,jitter]，抖动天然造成**乱序** |
| `network.loss` / `duplicate` | 每条消息独立的丢包 / 重复投递概率 |
| `requests[].at` | 客户端 `BEGIN` 到达时刻；`writes` 为该事务的 KV 写集（发给全部 3 个参与者） |
| `crashes[].node` | `coord` / `p1` / `p2` / `p3` |
| `crashes[].milestone` | 在指定协议里程碑崩溃（见下表），崩溃点**精确到 fsync 边界** |
| `crashes[].at` | 或按绝对 tick 崩溃（与 milestone 二选一） |
| `crashes[].downTicks` | 崩溃多久后重启；**0 表示整个运行期间永不恢复**（用于阻塞演示） |
| `dataDir` | WAL 落盘目录；留空则用临时目录，运行结束删除 |

投票策略：写集里出现 value 为 `@@NO`（或 key 以 `!` 开头）的写时，参与者投 NO，用于强制全局中止路径。

### 可注入崩溃的协议里程碑

| 里程碑 | 语义（崩溃发生在……之后、……之前） |
|---|---|
| `c-begin-wal:<txn>` | 协调者 `begin` 记录 fsync 之后、PREPARE 发出之前 |
| `c-prepare-sent:<txn>` | PREPARE 全部发出之后 |
| `c-vote-recv:<txn>` | 协调者每收到一票之后 |
| `c-votes-complete:<txn>` | 收齐 3 票之后、**决定记录 fsync 之前** |
| `c-decision-wal:<txn>` | COMMIT/ABORT 决定 **fsync 之后**、决策广播之前 |
| `c-ack-recv:<txn>` / `c-all-acked:<txn>` | 收到 ACK 之后 / 收齐全部 ACK 时 |
| `p-prepare-recv:<txn>` | 参与者收到 PREPARE、`prepared` fsync 之前 |
| `p-prepared-wal:<txn>` | `prepared` fsync 之后、YES 投票发出之前 |
| `p-vote-sent:<txn>` | YES 投票发出之后 |
| `p-commit-recv:<txn>` | 收到 COMMIT、commit 记录 fsync 之前 |
| `p-commit-wal:<txn>` | commit fsync 之后、应用并 ACK 之前 |
| `p-abort-wal:<txn>` | abort fsync 之后、ACK 之前 |

崩溃通过"钩子 + sentinel 展开当前处理函数"实现：钩子点之前已 fsync 的写留在磁盘上，
钩子点之后的发送/写绝不会执行——因此崩溃窗口是精确的，而不是近似的。

## 4. 协议实现要点

- **WAL 先写原则（write-ahead）**
  - 参与者：先 fsync `prepared`，再发 YES；先 fsync `commit`/`abort`，再改易失 KV、再 ACK。
  - 协调者：先 fsync `begin`（含参与者名单和写集），再发 PREPARE；先 fsync 全局决定，再广播 COMMIT/ABORT。
  - `internal/wal` 每条记录 `Flush + fsync`；重启时**只**靠重放日志重建状态，内存全部清零。
- **幂等与重传**：PREPARE、COMMIT、ABORT、VOTE、ACK 均按 txn 去重，重复投递无害；
  协调者对未决票/未 ACK 的决策周期性重传，因此 30% 丢包 + 25% 重复下事务仍能完成。
- **参与者准备后绝不单方面中止**：prepared 后只做两件事——等决策、周期性向协调者发 QUERY。
  代码中**没有任何"prepared 超时回滚"定时器**（见 `internal/twopc/participant.go` 的
  `startQueryLoop` 注释）。这是避免部分提交的安全红线。
- **协调者恢复（presumed-abort）**：重启重放 WAL——
  - 日志里已有 `commit`/`abort`：决定不可更改，按记录重新广播并持续重传直到 ACK 齐；
  - 日志里只有 `begin`（未决）：安全地补一条 ABORT。之所以安全，是因为"决定 fsync 早于决定发出"，
    日志中未决意味着任何决策消息都从未发出，不可能有参与者已经提交。
- **判定口径**：`internal/harness` 在运行结束后**直接读磁盘上的 WAL 文件**判定每节点状态，
  不信任内存。同一事务出现"某节点持久 commit + 另一节点持久 abort"才记为 `partial-commit!`
  并写入 `invariantErrors`；"部分已提交、其余仍 prepared"是合法的**阻塞**（全局决定是 COMMIT，
  只是未能送达），明确区分为 `blocked`。

## 5. 验收：在每个协议阶段崩溃重启 —— 无部分提交

下表为实际运行结果（seed=42，每处崩溃后 downTicks=40 重启，horizon=600）。
复现命令与自动化测试 `TestCrashAtEveryProtocolStage` 一致。

| 崩溃里程碑 | 重启后最终结果 | 说明 |
|---|---|---|
| coord `c-begin-wal` | **aborted** | 重启发现未决事务，presumed-abort |
| coord `c-prepare-sent` | **aborted** | 已 prepared 的参与者收到恢复后的 ABORT |
| coord `c-vote-recv` | **aborted** | 同上，决定从未落盘，安全中止 |
| coord `c-votes-complete` | **aborted** | 决定 fsync 前崩溃，恢复中止 |
| coord `c-decision-wal` | **committed** | COMMIT 已落盘，重启重放并重广播，3 方提交 |
| coord `c-ack-recv` | **committed** | 决定不可改，重传 COMMIT 补齐 ACK |
| coord `c-all-acked` | **committed** | 无影响 |
| p1 `p-prepare-recv` | **committed** | prepared 未落盘，协调者重传 PREPARE，重新准备 |
| p1 `p-prepared-wal` | **committed** | 重启后仍 prepared，QUERY 得知 COMMIT 后提交，**未自行回滚** |
| p1 `p-vote-sent` | **committed** | 票丢失则重传 PREPARE 后重投，决定后提交 |
| p1 `p-commit-recv` | **committed** | commit fsync 前崩溃，COMMIT 重传后提交 |
| p1 `p-commit-wal` | **committed** | commit 已落盘，重启即为已提交，重复 COMMIT 幂等 ACK |

**所有 12 个崩溃点 `invariantErrors` 均为空**：无任何节点出现 commit/abort 决策冲突，
无决策未落盘先发送。

## 6. 阻塞证据（不伪装高可用）

### 场景 08：协调者在 COMMIT 落盘后、广播前永久宕机

`scenarios/08-blocking-coord-down.json`（`downTicks: 0`，协调者永不恢复）。真实输出摘要：

```
txn T1   status=blocked   coordUp=false client=-
BLOCKED txn T1 prepared=[p1 p2 p3] coordUp=false queries=108
  reason: coordinator is DOWN; prepared participants cannot learn the decision and must not abort unilaterally
invariants: OK (no partial commit; decision durable before send)
```

完整轨迹见 `examples/output/08-blocking-coord-down.json`，其中关键时间线（虚拟 tick）：

```
10 durable coord record=begin  txn=T1
11 durable p3 record=prepared  txn=T1
12 durable p1 record=prepared  txn=T1
12 durable p2 record=prepared  txn=T1
14 durable coord record=commit txn=T1      <- 决定已持久化
14 crash   coord downTicks=0               <- 随即永久崩溃，COMMIT 从未发出
19 send p3 -> coord Query   ...            <- 参与者只能反复 QUERY
20 drop p3 -> coord Query reason=node-down <- 协调者宕机，查询全部被丢弃
...
query sends: 108（tick 19 一直持续到 tick 300）；node-down drops: 105
```

终态：3 个参与者全部停在 **PREPARED**，没有任何节点 commit 或 abort，KV 均未改动。
参与者正确地选择**阻塞等待**而非超时回滚——因为协调者磁盘上其实已有 COMMIT，
擅自回滚就会造成部分提交。这正面证明了 2PC 在协调者不可用时**可能阻塞**。

### 场景 10：参与者投 YES 后永久宕机（终止阶段阻塞）

```
status=blocked  committedNodes=[p2 p3]  preparedNodes=[p1]  abortedNodes=[]
11 durable p1 record=prepared; 11 crash p1 downTicks=0
14 durable coord record=commit; 15 durable p2/p3 record=commit
commit retransmits to p1: 37 ; drops to p1 (node-down): 37
```

全局决定是 COMMIT 且不可更改，p2/p3 正常提交；p1 永远停在 PREPARED，协调者重传 37 次
COMMIT 全部因 p1 宕机被丢弃。**仍无任何 abort，无原子性破坏**——这是合法的终止阻塞。

## 7. 场景清单（`scenarios/`，均可直接作为请求样例）

| 文件 | 预期终态 | 覆盖点 |
|---|---|---|
| 01-happy-commit | committed | 正常全提交 |
| 02-network-loss-dup-reorder | committed | 30% 丢包 + 20% 重复 + 抖动乱序，仅靠重传完成 |
| 03-vote-no-global-abort | aborted | 一票 NO => 全局中止 |
| 04-participant-crash-before-prepared | committed | prepared 前崩溃，无残留，重传后提交 |
| 05-participant-crash-after-prepared | committed | prepared 后崩溃重启，QUERY 后提交，不自行回滚 |
| 06-coord-crash-before-decision | aborted | 决定落盘前协调者崩溃，恢复 presumed-abort |
| 07-coord-crash-after-decision | committed | 决定落盘后崩溃，重启重放并完成提交 |
| 08-blocking-coord-down | **blocked** | 协调者永久宕机 => 全体 prepared 阻塞（证据） |
| 09-participant-crash-after-commit | committed | commit 落盘后崩溃，重启已提交，幂等 ACK |
| 10-blocking-participant-down | **blocked** | 参与者永久宕机 => 全局 COMMIT 下的终止阻塞（证据） |

## 8. 自动化测试

- `internal/engine`：同种子轨迹逐事件一致；崩溃期间消息被丢弃、崩溃前定时器失效；重启后恢复投递；崩溃 sentinel 精确中止处理函数。
- `internal/wal`：append/fsync/重放、缺失日志为空、重启后以追加方式打开（不截断）。
- `internal/harness`：10 个场景的端到端断言；**12 个协议里程碑逐个崩溃**的参数化测试；
  确定性（同种子两次运行 JSON 轨迹完全一致；8 个不同种子下不变量恒成立）；
  两个阻塞场景断言"确实阻塞且有 QUERY/重传证据，但绝无不变量破坏"。

### 如实运行记录

以下均为本机实际执行的原始输出（Go 1.22.2，linux/amd64）：

```
$ go test ./... -count=1
?       twopc-sim       [no test files]
ok      twopc-sim/internal/engine    0.003s
?       twopc-sim/internal/twopc     [no test files]
ok      twopc-sim/internal/harness   0.267s
ok      twopc-sim/internal/wal       0.012s

$ go test -race ./...
ok      twopc-sim/internal/engine    1.017s
ok      twopc-sim/internal/harness   1.371s
ok      twopc-sim/internal/wal       1.023s
```

10 个场景 CLI 逐个运行，终态与第 7 节表格一一对应（committed/aborted/blocked），
每个场景的摘要行都打印 `invariants: OK`；两个 blocked 场景给出了上文引用的查询/重传计数。

**未通过项 / 已知限制（如实说明）：**

1. 本项目没有，也按要求不做前端；交互方式只有 JSON 文件 CLI 与 Go 测试。
2. 模拟器是单进程、单协程的确定性模型；它**不**测量真实性能、真实 fsync 耗时或真实 TCP 行为，
   `fsync` 在此表达"写已具备崩溃持久性"这一语义，底层是普通本地文件。
3. 阻塞是 2PC 的固有性质而非缺陷：场景 08/10 中只要故障节点不恢复，事务就会一直阻塞到
   `horizon`。本项目不实现 3PC/Paxos 等非阻塞协议（那会改变题目指定的 2PC 语义）。
4. 目前场景以单事务为主（足以精确刻画各崩溃窗口）；引擎与状态机本身支持多事务，
   多事务压力场景未列入验收集。
5. 未发现未通过的测试；如修改协议代码，`invariantErrors` 字段会在输出中如实暴露任何
   决策顺序/原子性破坏，而不会让进程以失败退出混淆"仿真跑通"和"协议正确"。
