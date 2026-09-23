# raftrun —— Raft 日志安全子集 · 确定性离散事件模拟器

纯后端项目。在**单进程**内用确定性离散事件模拟（DES）驱动固定 3 节点的
Raft 集群，实现**选举**与**日志复制**两个核心机制，不含成员变更与快照。
虚拟网络支持**丢包、重复、乱序**；节点可崩溃并从磁盘**重启恢复**；
不依赖任何真实集群、网络监听或协程定时器。

> 安全性约束：提交规则仅限**当前任期**条目；多数派失联时不会确认任何写入。
> 每次运行结束都会断言“所有存活节点已提交索引上的内容（任期、命令）逐格一致”。

---

## 1. 目录结构

```
.
├── cmd/raftrun/main.go          # 命令行入口：读场景 JSON -> 模拟 -> 输出结果 JSON
├── internal/
│   ├── raft/
│   │   ├── raft.go              # Raft 节点核心（选举 / 日志复制 / 提交规则）
│   │   ├── storage.go           # 持久化接口 + 文件实现（任期/投票/日志，原子写）
│   │   ├── raft_test.go         # 节点级单元测试
│   │   └── helpers_test.go
│   └── sim/
│       ├── scenario.go          # 场景 JSON 结构与校验
│       ├── eventqueue.go        # (时间, 序号) 确定性最小堆事件队列
│       ├── simulator.go         # 模拟器装配、节点运行时
│       ├── engine.go            # 主循环、虚拟网络、外部事件、不变量检查
│       ├── sim_test.go          # 端到端固定事件序列测试
│       └── random_test.go       # 随机化（丢包/分区/崩溃重启）安全压力测试
├── examples/                    # 请求样例（场景 JSON）
│   ├── basic-replication.json
│   ├── old-leader-revival.json  # 旧领导者复活 + 日志冲突
│   ├── crash-restart.json       # 崩溃 / 重启 / 全集群重启
│   ├── majority-outage-window.json   # 多数派失联窗口：写入不确认
│   ├── majority-unavailable.json     # 失联后恢复：挂起写入随后确认
│   └── flaky-network.json       # 20% 丢包 + 5% 重复 + 乱序
└── README.md
```

## 2. 工作原理（确定性）

- **事件队列**：所有事件按 `(时间, 单调序号)` 排序，同刻事件严格 FIFO。
  没有任何 goroutine、`time.Sleep` 或真实 IO 等待，因此给定
  `(场景, seed)` 输出**逐字节确定**（测试中有断言）。
- **节点**：Raft 节点只实现纯状态机，通过 `raft.Environment` 接口
  （`Send` / `ScheduleElection` / `ScheduleHeartbeat` / `Now`）与模拟器交互。
- **虚拟网络**：每条消息独立按概率**丢弃**、按概率**复制一份**，
  并在基础时延上叠加随机抖动，独立抖动使副本可能早于/晚于原件到达
  （制造**乱序**与**重复**）。`partition` / `heal` / `link` 事件可断开、
  恢复或调整双向链路。
- **定时器失效**：
  - 每次重排选举定时器都会更换 `nonce`，旧超时事件自动失效，
    避免一次任期内堆积多个超时导致无谓改选；
  - 节点每次（重新）启动更换 `epoch`，崩溃前在途的定时器与消息全部失效。
- **崩溃与重启**：`crash` 立即终止节点（内存状态丢弃、在途事件失效）；
  `restart` 用**同一磁盘目录**重建节点，节点从 `state.json` 恢复
  `currentTerm` / `votedFor` / `log`。持久化采用
  “临时文件 + `rename`”保证单次保存原子性。

### 实现的 Raft 规则

| 机制 | 实现要点 |
| --- | --- |
| 选举 | 随机选举超时 → 候选人自投并拉票；多数票（2/3）当选；日志新旧比较（先末条任期、再末条索引）决定是否投票 |
| 心跳 | 领导者周期广播空 AppendEntries，跟随者收到合法心跳即重置选举定时器 |
| 日志复制 | `prevLogIndex/prevLogTerm` 一致性检查；冲突时回退 `nextIndex` 并截断跟随者分叉日志 |
| 提交规则 | **只提交当前任期**的条目（安全子集），旧任期条目随当前任期条目一起被间接提交 |
| 多数派失联 | 存活的旧领导者无法凑齐多数派，写入只停留在本地日志（`accepted`），不会变成 `committed` |

不实现：成员变更、快照、日志压缩、真实客户端会话、线性化读。

## 3. 构建与运行

需要 Go（开发使用 go1.27.x；仅用标准库，无第三方依赖）。

```bash
go build ./...
go run ./cmd/raftrun -scenario examples/old-leader-revival.json
# 或先编译
go build -o raftrun ./cmd/raftrun
./raftrun -scenario examples/basic-replication.json > result.json
```

退出码：

| 码 | 含义 |
| --- | --- |
| 0 | 运行完成且安全断言通过 |
| 1 | 读/解析场景或运行出错 |
| 2 | 参数错误 |
| 3 | 运行完成但**安全断言失败** |

## 4. 请求（场景 JSON）字段

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `name` | string | 场景名 |
| `nodeCount` | int | **固定为 3** |
| `seed` | int | 随机种子（选举抖动、网络行为） |
| `endTime` | int | 模拟终止时刻（毫秒，含此前事件） |
| `heartbeat` | int | 心跳周期（ms） |
| `electionTimeout` | `{min,max}` | 随机选举超时闭区间（须 > heartbeat） |
| `network` | object | `baseDelay`(ms) / `jitterPct`(0~1) / `lossPct`(0~1) / `dupPct`(0~1) |
| `dataDir` | string | 持久化根目录（运行开始清空节点状态文件） |
| `nodes` | object? | 按节点覆盖时序，如 `"1": {"electionMin":120,"electionMax":120}` 固定节点1先超时 |
| `trace` | bool | 是否在结果中输出消息/故障轨迹 |
| `events` | array | 脚本事件，见下 |

事件类型（`type`）：

- `client`：`node` 可为 `1/2/3` 或 `"leader"`（自动解析为当前最高任期在任领导者）；
  需要 `client`（请求唯一标识）与 `command`。
- `crash` / `restart`：`node` 为目标节点。
- `partition`：`groups` 为分区分组，如 `[[1],[2,3]]` 表示隔离节点 1。
- `heal`：恢复全部被断开的链路。
- `link`：`from`/`to` + 可选 `block`/`loss`/`dup`，调整双向链路参数。

`client` 写入的 `status`：

| 状态 | 含义 |
| --- | --- |
| `committed` | 已被多数派确认并出现在全局已提交前缀（最终状态） |
| `accepted` | 被领导者追加到本地日志，但截至模拟结束**未获多数派确认** |
| `notLeader` | 目标节点存活但不是领导者，写入被拒 |
| `nodeDown` | 目标节点已崩溃（或当前无领导者） |

## 5. 结果（输出 JSON）

`committed` 为观察到的全局已提交日志（索引、任期、命令、客户端）；
`nodeCommitIndex` / `nodeLogLength` 为各存活节点最终状态；
`leaderChanges` 为领导者更迭记录；`invariantCheck.passed` 为安全断言结果。
`trace=true` 时 `trace` 给出 `send`/`deliver`/`drop`/`crash`/`restart`/
`partition`/`heal` 事件序列（重复副本以 `detail:"duplicate"` 标记）。

## 6. 自动化测试

```bash
go test ./...                 # 全部测试
go test -race ./...           # 加竞态检测
go test -short ./...          # 随机压力场景缩减到 8 个种子
go test -v ./internal/sim -run TestOldLeaderRevival
```

测试覆盖：

- **节点单元测试**：多数派选举、拒绝旧任期追加、当前任期提交规则
  （含“旧条目单独多数派不得提交”负向用例）、冲突截断、落盘恢复。
- **固定事件序列端到端**：旧领导者复活与日志冲突覆盖、领导者/全集群
  崩溃重启、多数派失联窗口写入不确认、失联恢复后继续提交、确定性复跑、
  弱网安全性，以及 CLI 构建/退出码。
- **随机化安全压力测试**：200 个随机种子，随机丢包/重复/乱序 +
  随机分区 + 随机崩溃-重启 + 随机写入，逐场景断言安全性不变量。

### 结束时的安全不变量

1. 所有存活节点的**已提交前缀**在索引、任期、命令上逐格一致；
2. 日志匹配性质：同索引且同任期则命令相同，单节点日志任期序列不倒退；
3. 所有报告为 `committed` 的写入确实位于全局已提交前缀且命令一致。

## 7. 关键场景说明：旧领导者复活（`old-leader-revival.json`）

固定节点 1 选举超时最短，使其成为初始领导者：

1. `t=300` 向领导者写入 `c1` → 已提交；
2. `t=500` 分区 `[[1],[2,3]]`，旧领导者节点 1 被隔离；
3. `t=900` 仍向节点 1 写入 `c2`：本地 `accepted`，**永远不会被提交**；
4. 多数派一侧 `t≈765` 选出节点 2 为新领导者（更高任期），
   `t=1100` 的写入 `c3` 在多数派提交；
5. `t=1600` 愈合：节点 1 收到更高任期 AppendEntries，退回跟随者，
   其分叉的旧任期条目被**截断覆盖**；随后 `c4/c5` 正常提交；
6. 结束时三节点日志完全一致，且 `c2` 的命令不出现在任何已提交索引上。
