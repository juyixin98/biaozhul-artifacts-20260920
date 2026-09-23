# vccsim — 向量时钟冲突检测模拟器

纯后端、单进程、确定性的离散事件模拟器：多个"节点"在同一进程内运行版本化寄存器，
通过向量时钟判定版本之间的**先后 / 并发**关系，网络可配置**丢包、重复、乱序**。
**不依赖任何真实集群、网络或外部服务**（无 goroutine per node、无 socket 互连）。

## 要解决的问题

多副本离线写入会产生分叉。朴素策略"最后写入获胜"会**静默丢失并发版本**。
本项目验证：

1. **离线写入**产生的并发版本在收敛时被同时保留为 sibling；
2. **重复传输**幂等，旧版本不会复活；
3. 客户端**合并写入必须携带它所覆盖的完整上下文（context）**——
   上下文不完整（漏看并发兄弟）或过旧（基于已被取代的祖先）时，写入被**拒绝**，
   从机制上杜绝"旧上下文覆盖新并发版本"。

## 目录结构

```
.
├── go.mod
├── main.go                         # CLI: run / serve（JSON 运行接口）
├── internal/
│   ├── clock/clock.go              # 向量时钟：tick / merge / 偏序比较
│   ├── register/register.go        # 版本化寄存器 + 完整上下文合并校验
│   └── sim/sim.go                  # 确定性离散事件模拟器（时间堆 + 种子 RNG）
├── examples/                       # 验收场景（请求样例）
│   ├── 01-offline-writes.json
│   ├── 02-duplicate-drop-reorder.json
│   ├── 03-stale-context-merge.json
│   ├── 04-probabilistic-network.json
│   └── http-requests.sh
├── reports/                        # 实际运行产生的报告（已随仓库保存）
├── RUN_LOG.md                      # 实际命令与结果的如实记录（含未通过项）
└── README.md
```

## 快速开始

需要 Go 1.22+（开发使用 go1.22.2），无第三方依赖。

```bash
go test ./...                      # 全部自动化测试
go run . run -file examples/01-offline-writes.json          # 运行场景，报告打到 stdout
go run . run -file examples/03-stale-context-merge.json -out report.json
cat scenario.json | go run . run                            # 也可从 stdin 读

go run . serve -addr :8080         # HTTP 模式
curl -X POST localhost:8080/run -d @examples/01-offline-writes.json
```

`run` 在场景内 `inspect` 断言失败时退出码为 **1**（报告仍正常写出），
便于接入 CI。

## 核心概念

### 向量时钟偏序

对两个向量时钟 `a`、`b`：

| 关系 | 判定 | 含义 |
|---|---|---|
| `a` 先于 `b` | `∀k: a[k] ≤ b[k]` 且至少一处严格小于 | `b` 因果上包含 `a` |
| `a` 后于 `b` | 反向 | `a` 更新 |
| 相等 | 各分量相等 | 同一因果点 |
| **并发** | 互有大小（不可比） | 分叉，**双方都必须保留** |

### 版本化寄存器

每个 key 保存一个**极大版本集合（siblings）**：

- 写入：节点本地时钟 tick，新版本挂当前向量时钟；
- 接收版本：合并对端时钟；若新版本因果上支配已有 sibling 则 prune 旧版本，
  若是并发则加入集合，**相同版本 ID 重复投递为 no-op**，已被支配的旧版本不复活；
- 合并写入（`client_merge`）：客户端必须在 `context` 中列出它读取过的**全部 sibling**。
  系统取 context 向量时钟的 join，要求它支配当前**每一个** sibling：
  - 漏掉当前并发 sibling → `context does not cover concurrent siblings`（拒绝）；
  - context 是当前 sibling 的严格祖先（拿着旧读取结果）→ `stale context`（拒绝）；
  - 全部覆盖 → 接受，本地 tick 产生合并版本，旧 siblings 被其因果支配而收敛。

> 设计要点：这不是"乐观并发再报错"的拍脑袋检查，而是因果偏序的直接应用——
> context join 不支配的版本，就是客户端合并逻辑从未见过的版本，覆盖它必然丢数据。

### 确定性模拟器

- 单一全局虚拟时间，事件放在最小堆（按 `(time, seq)` 排序），顺序执行，无并发；
- 所有随机决策来自 `rand.New(rand.NewSource(seed))`，**相同 seed + 场景产生字节级一致的报告**
  （有专门测试）；
- 网络策略：`drop_prob` / `dup_prob` / `reorder_prob`（概率）或事件级
  `force_drop` / `force_dup` / `force_reorder`（强制，用于确定性验收）；
- 节点可 `offline`/`online`，节点对之间可 `partition`（断网/愈合）；
- 离线或分区期间到达的消息不生效（trace 如实标记），重传由场景显式驱动。

## JSON 接口

### 请求（Scenario）

```jsonc
{
  "name": "my-scenario",
  "seed": 42,                        // 随机种子，决定概率故障
  "nodes": ["A", "B"],
  "network": {
    "drop_prob": 0.1, "dup_prob": 0.1, "reorder_prob": 0.1,
    "base_delay": 1, "jitter": 1, "reorder_delay": 4
  },
  "max_time": 100,
  "events": [ /* 见下 */ ]
}
```

事件类型：

| type | 必填字段 | 说明 |
|---|---|---|
| `write` | `time,node,key,value` | 节点本地（可离线）写 |
| `send` | `time,from,to,key` | 投递源节点该 key 的全部当前 siblings；可加 `force_drop/dup/reorder` |
| `offline` / `online` | `time,node` | 节点下线/上线 |
| `partition` | `time,from,to` | 默认断开该节点对；`"connect": true` 愈合 |
| `client_merge` | `time,node,key,value,context[]` | 带完整上下文的客户端合并写 |
| `inspect` | `time,node,key` | 快照断言：`expect_siblings`、`expect_ids[]` |

`context` 条目：`{"id": "A:2", "vc": {"A": 2}}`（`vc` 可省略，模拟器从历史解析）。

### 响应（Report）

```jsonc
{
  "name": "...", "seed": 42,
  "trace": [               // 按虚拟时间排序的完整事件轨迹
    {"time": 1, "kind": "write", "node": "A", "key": "k",
     "version_id": "A:1", "siblings": [ /* 写入后的极大版本集合 */ ]},
    {"time": 5, "kind": "send", "from": "A", "to": "B",
     "dropped": false, "duplicated": true, "reordered": false},
    {"time": 6, "kind": "receive", "from": "A", "to": "B",
     "version_id": "A:1", "accepted": false,
     "detail": "duplicate or obsolete", "siblings": [ /* ... */ ]},
    {"time": 8, "kind": "client_merge", "accepted": false,
     "error": "context does not cover concurrent siblings: [A:2]"}
  ],
  "final": [               // 每个节点最终时钟与各 key 的 siblings
    {"node": "B", "online": true, "clock": {"A": 2, "B": 2},
     "keys": {"k": [ { "id": "B:2", "value": "merged-ab", "vc": {"A":2,"B":2} } ]}}
  ],
  "assertions_ok": true,
  "assertion_errors": []
}
```

## 四个场景

| 场景 | 验证内容 |
|---|---|
| `01-offline-writes` | B 离线下两节点各写一次，恢复后互发，双方均保留 **2 个并发 siblings**（`A:1`,`B:1`） |
| `02-duplicate-drop-reorder` | 强制重复（投递 2 次只 ingest 1 次）、强制丢包（无 receive）、强制乱序延迟（重复/旧版本不改变状态） |
| `03-stale-context-merge` | 漏看并发 sibling 的合并被拒、基于旧祖先的合并被拒、两个 sibling 原封不动；完整上下文合并成功并收敛为 1 个版本 |
| `04-probabilistic-network` | 3 节点、20% 丢/重/乱序概率下多轮 gossip，跨 40 个种子全部收敛到 3 个 siblings，无一丢失 |

实际运行的命令、输出摘要和曾出现的未通过项见 **[RUN_LOG.md](RUN_LOG.md)**。

## 设计边界（明确不做的事）

- 不做真实网络、服务发现、持久化、前端；
- 不做向量时钟之外的冲突解决策略（无 LWW、无 CRDT 合并语义）——
  值合并由客户端负责，服务端只保证"不静默丢版本"和"上下文校验"；
- 概率丢包下"最终收敛"需要场景持续重传（反熵）；有限重传轮数可能仍不收敛，
  这是对现实的忠实模拟，`inspect` 会如实报错而非假装成功。
