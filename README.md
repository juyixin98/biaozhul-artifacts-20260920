# 分片屏障快照模拟器（Chandy–Lamport Snapshot）

纯后端、单进程、确定性的离散事件模拟器。节点（银行“分片”）在一个进程内通过
**模拟网络**通信，网络可以丢包、重复、乱序；**不依赖任何真实集群、网络或时钟**。
在 **可靠 FIFO 信道**上实现 [Chandy–Lamport 分布式快照算法](https://en.wikipedia.org/wiki/Chandy%E2%80%93Lamport_algorithm)，
用“转账守恒模型”验收：任意已完成快照中

```
Σ 节点记录余额(含补记 out_after) + Σ 在途转账金额 = 初始总额（恒定）
```

多个快照 ID 可以交叠进行，状态彼此隔离、互不串台。

---

## 目录结构

```
.
├── go.mod
├── cmd/simsnap/            CLI 入口（JSON 进 / JSON 出）
├── internal/
│   ├── msg/                共享消息类型（Data / Marker）
│   ├── sim/                确定性离散事件引擎 + 模拟网络（延迟/丢包/重复/乱序/FIFO）
│   ├── bank/               转账守恒模型节点（发方扣款、收方入账、TxnID 幂等去重）
│   ├── snap/               Chandy–Lamport 快照管理器（多快照交叠、在途记录、out_after 补记）
│   └── app/                JSON 请求装配、运行、结果汇总
└── examples/               请求样例（*.json）与参考输出（output/*.out.json）
```

无第三方依赖，仅用 Go 标准库。要求 Go 1.22+（`go.mod` 声明 1.22）。

---

## 构建与运行

```bash
go build ./...
go test ./...

# 文件输入 / 输出
go run ./cmd/simsnap -in examples/01-inflight.json -out /tmp/resp.json

# 标准输入 / 标准输出
cat examples/03-overlapping.json | go run ./cmd/simsnap
```

退出码：参数或 JSON 非法时打印错误并以非零码退出。

---

## 请求 JSON 格式

```jsonc
{
  "seed": 42,                        // 随机种子，决定延迟/丢包/重复/随机流量（同种子完全可复现）
  "balances": [100, 60, 40],         // 各节点初始余额；长度 = 节点数（全互联拓扑）
  "default_policy": {                // 未在 links 中显式给出的链路使用此策略
    "min_delay": 20, "max_delay": 20,
    "loss_pct": 0, "dup_pct": 0,
    "fifo": true
  },
  "links": [                         // 可选：逐链路覆盖 default_policy
    {"a": 0, "b": 1, "policy": {"min_delay": 1, "max_delay": 30, "fifo": false}}
  ],
  "transfers": [                     // 脚本化转账（at 时刻 from -> to 金额 amount）
    {"at": 4, "from": 0, "to": 1, "amount": 25}
  ],
  "snapshots": [                     // 脚本化快照发起（at 时刻由 initiator 发起 id）
    {"at": 2, "id": 1, "initiator": 0}
  ],
  "traffic": {                       // 可选：[from,until] 内每 every 拍发起一笔随机转账
    "from": 5, "until": 120, "every": 2, "max_amount": 30
  },
  "deadline": 80                     // 仿真截止逻辑时刻；启用 traffic 时必填（保证终止）
}
```

### 链路策略 `policy`

| 字段 | 含义 |
|---|---|
| `min_delay` / `max_delay` | 传播时延范围（逻辑时间单位，闭区间，随机均匀抽取） |
| `loss_pct` | 丢包概率，0–100 |
| `dup_pct` | 重复投递概率，0–100（业务层按 `(from,txn_id)` 幂等去重） |
| `fifo` | 是否保证同一方向严格按发送顺序投递 |

> **Chandy–Lamport 快照只在可靠（`loss_pct=0`）且 FIFO 的信道上成立。**
> 标记（Marker）与业务消息走同一条信道；非 FIFO / 丢包样例作为反面对照。

---

## 响应 JSON 关键字段

```jsonc
{
  "initial_total": 200,
  "final_total": 200,                // 仿真结束时各节点余额之和（无丢包时 = 初始总额）
  "final_balances": [{"node":0,"balance":75}, ...],
  "snapshots": [{
    "snapshot": {
      "id": 1, "complete": true, "complete_at": 43,
      "nodes": [
        {"node":0, "taken_at":2, "state":{"balance":100}, "out_after":0}
      ],
      "channels": [                  // 在记录窗口内捕到过消息的入信道
        {"from":2,"to":1,"closed":true,"msgs":[{"amount":10,"sent_at":4,...}]}
      ]
    },
    "node_sum": 190,                // Σ 节点记录余额（含 out_after 补记）
    "in_flight": 10,                // Σ 在途转账金额（网络重复副本按 (from,txn_id) 去重）
    "total": 200,                   // node_sum + in_flight
    "expected": 200,
    "conserved": true               // complete && total == initial_total
  }],
  "network": [{"from":0,"to":1,"stats":{"sent":3,"delivered":3,"lost":0,"duplicated":0,"reordered":0}}],
  "events": [ /* 完整时间线：send/deliver/marker_*/snap_*/drop_dup ... */ ]
}
```

### 为什么有 `out_after` 补记

节点记录本地状态时，此前发起的转账可能还没到对端。FIFO 下：

- 在对端**记录状态之前**到账 → 已进对端余额；
- 在对端**记录之后、入信道标记截止之前**到账 → 计入对端的在途消息（`channels`）；
- 在入信道标记**截止之后**才到账（或快照完成时仍在网络）→ 既不在发方记录余额
  （发起即扣款）也不在对端状态/在途里。这部分由快照管理器依据发送/投递日志
  补记为发方的 `out_after`，保证切面上每笔钱恰好归属一次。

---

## 算法要点

1. 发起节点记录本地状态，沿所有出信道发 **Marker**；
2. 节点首次收到某快照的 Marker：记录本地状态，向所有出信道转发 Marker；
3. 节点在“已记录状态”与“收到某邻居 Marker”之间收到的业务消息，记入对应入信道；
4. 每个节点都记录状态、每条入信道都收到 Marker → 快照完成；
5. 多个快照 ID 交叠时，节点为每个 ID 维护独立的状态与入信道记录。

单线程事件循环 + 全局事件堆（时刻 → 优先级 → 插入序号）保证调度确定；
所有随机量（延迟、丢包、重复、随机流量）来自与引擎绑定的可播种 `rand.Rand`。

---

## 样例与实测结果

以下为在本机实际运行 `go run ./cmd/simsnap -in examples/<file>` 的结果（Go 1.22.2/linux-amd64）。

| 样例 | 场景 | 最终总额 | 快照结果 |
|---|---|---|---|
| `01-inflight.json` | 固定延迟，快照窗口内确有在途转账 | 200 | snap1 **守恒 total=200**（在途 $10） |
| `02-empty-channels.json` | 空信道（业务落定后才拍快照） | 200 | snap1 **守恒 total=200**，在途=0 |
| `03-overlapping.json` | 4 节点、3 个快照标记交错 | 400 | snap1/2/3 全部 **守恒 total=400** |
| `04-continuous-traffic.json` | 快照期间持续随机转账 | 2000 | snap1 **守恒 total=2000** |
| `05-traffic-overlap.json` | 持续流量 + 两快照交叠 | 900 | snap100/200 全部 **守恒 total=900** |
| `06-nonfifo.json` | 非 FIFO（反面对照） | 600 | `reordered=8`；快照切面不保证守恒（total=610） |
| `07-duplicate-fifo.json` | 可靠 FIFO + 50% 重复投递 | 300 | snap1 **守恒 total=300**（业务幂等，`drop_dup`） |
| `08-lossy.json` | 100% 丢包（反面对照） | 170 | 快照**永不完成**（Marker 收不齐），`lost=4` |

完整响应见 `examples/output/*.out.json`。

---

## 自动化测试

```bash
go test ./...            # 全部包
go test -race ./...      # 竞态检测
go test -cover ./internal/...
```

覆盖内容：

- `internal/sim`：确定性回放、Stop/截止时刻、FIFO 保序、非 FIFO 乱序统计、丢包/重复数量关系；
- `internal/snap`：基本在途捕获、**多快照交叠互不串台**、`out_after` 四种时序、重复发起幂等；
- `internal/bank`：扣款/入账守恒、重复幂等、不同发送方同事务号、余额不足、非正金额；
- `internal/app`：静态守恒、**空信道**、**标记交错**、**快照期间持续消息**、持续流量+交叠、
  确定性重放、非 FIFO 反例、重复 FIFO、丢包快照不完成、状态冻结、参数校验，以及对全部
  `examples/*.json` 的黄金用例校验；
- `cmd/simsnap`：编译二进制，文件/标准输入两种 I/O、非法 JSON 非零退出。

实测覆盖率：app 88.5%、bank 91.3%、sim 89.2%、snap 82.7%。
