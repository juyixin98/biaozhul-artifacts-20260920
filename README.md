# Raft 日志安全子集 —— 确定性离散事件模拟器

纯后端 Go 项目。在**单进程**内用离散事件模拟固定 3 节点 Raft 集群，
通过 JSON 脚本注入事件（客户端写、崩溃/重启、网络分区、丢包/重复/乱序），
执行结束输出 JSON 结果，并在每个 tick 自动检查 Raft 安全不变量。
**不依赖任何真实集群、网络或时钟，无第三方依赖。**

## 实现范围

对应 Raft 论文（Ongaro & Ousterhout, 2014）的"安全子集"：

- 固定 3 节点，多数派 = 2；**无**成员变更、**无**快照、**无**日志压缩。
- 领导者选举：随机化选举超时、RequestVote、选举限制（§5.2 的"日志至少一样新"）。
- 日志复制：AppendEntries（兼作心跳）、一致性检查、冲突任期快速回退与截断（§5.3）。
- **提交规则只提交当前任期条目**（§5.4.2）；领导者当选先写一条 noop，
  使之前任期已复制的条目随之提交（§8 标准做法）。
- 持久化 `currentTerm` / `votedFor` / `log`：在响应任何 RPC 或确认客户端写之前落盘（§5.3）。
  - `memory` 存储：进程内模拟磁盘（崩溃保留，`wipe` 才清空）；
  - `file` 存储：真实 JSON 文件 + 临时文件 rename 原子写，可跨进程恢复。
- 网络故障：固定延迟、随机抖动（乱序）、丢包率、重复率，全部由种子决定，**结果可复现**。
- **多数派失联时，写入不会被确认**；旧领导者恢复后自动下台，其陈旧写入被覆盖。

## 目录结构

```
.
├── go.mod
├── cmd/raftsim/main.go        # JSON 运行接口（CLI）
├── internal/
│   ├── logstore/store.go      # 持久化：内存 + 文件（原子写）
│   ├── raft/raft.go           # Raft 节点状态机（选举/复制/提交/崩溃重启）
│   ├── network/network.go     # 丢包/重复/乱序的确定性模拟网络
│   └── sim/                   # 脚本模型 + 离散事件引擎 + 断言
│       ├── script.go
│       └── engine.go
├── examples/                  # 请求样例（脚本）与实际运行输出
│   └── output/*.result.json
└── README.md
```

## 快速开始

需要 Go 1.22+（仅用标准库）。

```bash
go build ./...
go test ./...

# 运行一个事件脚本，结果打印到 stdout
go run ./cmd/raftsim -f examples/01-old-leader-revival.json

# 或从标准输入
cat examples/01-old-leader-revival.json | go run ./cmd/raftsim -stdin

# 需要运行轨迹时
go run ./cmd/raftsim -f examples/02-log-conflict.json -trace 300
```

退出码：`0` 全部断言通过；`1` 有断言/安全不变量失败（stdout 仍是合法 JSON，
见 `failures`）；`2` 输入或配置错误（错误信息在 stderr）。

## JSON 请求格式（脚本）

顶层字段：

| 字段 | 说明 | 默认 |
|---|---|---|
| `seed` | 随机种子（选举抖动/网络抖动/丢包/重复） | 必填语义，建议显式给 |
| `nodes` | 节点数，固定为 `3` | 3 |
| `ticks` | 运行 tick 数（含边界） | 必填 |
| `heartbeat` | 心跳间隔（tick） | 3 |
| `election_lo` / `election_hi` | 选举超时区间 `[lo,hi)` | 8 / 16 |
| `node_windows` | 按节点覆盖选举窗口，用于编排确定性选举 | — |
| `storage` | `memory` 或 `file` | memory |
| `state_dir` | `file` 存储根目录（每节点一个 `nodeN/` 子目录） | — |
| `fresh_state` | `file` 存储时启动前清空 `state_dir`，保证样例可重复运行 | false |
| `network` | `{delay_ticks, jitter, loss_rate, dup_rate}` | 延迟 1，无故障 |
| `events` | 事件数组，见下 | — |

事件类型：

- `client_write`：`{at, kind, client, data, node?}`；不写 `node` 时发给当前最高任期领导者。
- `crash` / `restart` / `wipe`：`{at, kind, target}`。`crash` 保留磁盘，`wipe` 清空磁盘。
- `isolate`：`{at, kind, isolated, heal?}` 隔离/恢复单个节点的双向链路。
- `partition`：`{at, kind, partition:[g0,g1,g2], heal?}` 任意分区。
- `link`：`{at, kind, from, to, link:{...}|null}` 设置/恢复单条单向链路。
- `network_reset`：恢复全部链路默认参数。
- 断言：
  - `committed`：`{at, kind:"committed", client, data, expect_index}`，
    要求所有存活节点的已提交索引 `expect_index` 处为 `data` 且该写已被确认；
  - `not_acked`：要求该 client 的写**尚未**被确认（多数派失联场景）；
  - `log_contains`：`{target, data:"a b c", full_log?}`，日志按序包含这些数据；
  - `log_match`：所有存活节点的完整日志逐索引一致；
  - `commit_index`：`{target?, expect_index}` 检查提交点（不给 target 查全部存活节点）。

## JSON 响应格式

```json
{
  "ok": true,
  "ticks_run": 42,
  "term_history": [{"term": 1, "leader": 0}, {"term": 2, "leader": 1}],
  "acks": [
    {"client": "c1", "data": "one", "leader": 0, "term": 1, "applied_at": 10, "index": 2}
  ],
  "nodes": [
    {"id": 0, "alive": true, "role": "follower", "term": 2,
     "commit_index": 4, "committed": [ ... ], "full_log": [ ... ]}
  ],
  "network": {"dropped": 0, "duplicated": 0},
  "failures": []
}
```

索引 0 是哨兵；领导者当选写入的 noop 条目 `data` 为空字符串。

## 内置安全不变量（引擎每个 tick 自动检查）

1. **选举安全**：任一个任期至多出现一个领导者。
2. **已提交日志一致**：任意时刻任意两个存活节点，其共同已提交前缀逐索引（任期+数据）一致。
3. **确认安全**：运行结束时每条已确认的写入仍存在于多数派节点日志的同一索引。
4. 脚本断言（上述 `committed` / `not_acked` / `log_match` / …）。

## 验收场景（examples/，均实际运行通过）

| 脚本 | 覆盖内容 |
|---|---|
| `01-old-leader-revival.json` | 旧领导者隔离期间在旧任期写入（永不确认）；更高任期 leader 产生提交；隔离恢复后旧 leader 下台、陈旧写入被覆盖，所有节点已提交索引内容一致 |
| `02-log-conflict.json` | 领导者崩溃、追赶重启、follower 隔离期间反复选举抬高任期；恢复后同索引不同任期日志被截断覆盖，最终 `log_match` |
| `03-restart-persistence.json` | **真实文件持久化**：多数派崩溃时写入不确认；多次崩溃/重启后从磁盘恢复任期、投票与日志并继续提交 |
| `04-lossy-network.json` | 15% 丢包 + 10% 重复 + 抖动乱序下连续 5 次写入，最终日志一致 |
| `05-majority-lost.json` | 两节点崩溃期间旧领导者的写停留在未提交状态（`not_acked`，commitIndex 不动）；恢复后才提交，之后的写正常 |
| `06-assertion-failure.json` | 演示断言失败时 `ok=false`、退出码 1（在选出 leader 前就要求提交） |

`examples/output/` 保存了上述脚本在本机的实际运行结果。

## 自动化测试

```bash
go test ./...            # 全部单元 + 集成 + 24 种子随机混沌安全测试
go test -race ./...      # 竞态检测
go test -v -count=1 ./internal/sim -run TestRandomizedSafety
```

测试分层：

- `internal/logstore`：深拷贝隔离、文件往返（跨"进程"恢复）、清盘。
- `internal/network`：同种子确定性、丢包/重复计数、分区/隔离/恢复、乱序存在性。
- `internal/raft`：单 leader 选举、复制与当前任期提交、§5.4.2 提交限制、
  §5.3 冲突截断、崩溃后任期/选票持久化、旧 leader 见高任期下台。
- `internal/sim`：基本流程、确定性（两次运行 JSON 逐字节相同）、多数派失联不确认、
  跨进程文件持久化、清盘追赶、非法脚本拒绝，以及 **24 个随机种子的混沌安全测试**
  （随机崩溃/重启/隔离/丢包/写入，愈合后必须 `log_match`，全程三个不变量成立）。

## 设计取舍说明

- **为什么当选要写 noop**：论文 §5.4.2 规定不能仅凭副本数提交旧任期条目。
  新 leader 当选时先追加一条当前任期 noop，noop 复制到多数派后，
  其日志中此前任期的条目随之合法提交，避免提交点停滞。
- **为什么客户端写可能出现在"非最终"索引**：脚本事件在每 tick 的网络投递之前执行，
  与领导者变更恰好同 tick 时，写可能投给稍后才下台的旧 leader。这是真实的 Raft
  语义——该写不会被确认，并会在网络恢复后被新 leader 的日志覆盖。`01` 场景即利用这一点。
- 模拟时间是离散 tick，消息延迟至少 1 tick；同 tick 内按全局发送序号投递，保证确定顺序。
