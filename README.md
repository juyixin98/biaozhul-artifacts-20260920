# 分片屏障快照模拟器（Chandy–Lamport Snapshot）

一个**纯后端、单进程**的确定性离散事件模拟器，用 Go 实现
[Chandy–Lamport 全局快照算法](https://en.wikipedia.org/wiki/Chandy%E2%80%93Lamport_algorithm)，
以"转账守恒模型"验收快照正确性，并提供 JSON 请求/响应接口。

- 没有任何真实集群、真实网络、真实时钟或 goroutine 间通信：
  所有节点是进程内的数据结构，所有"时间"是逻辑 tick，全部随机由 `seed` 决定。
- 网络模型支持**可靠 FIFO 信道**（算法前提）与 **best_effort 信道**
  （可丢包、重复、乱序），后者用于演示算法前提被破坏时的现象。
- 多个快照 ID 可以**交叠并发**，每个节点为每个快照 ID 维护独立的本地记录，互不串台。

## 目录结构

```
cmd/snapshotsim/main.go          命令行入口（JSON in / JSON out）
internal/simulator/
  types.go                       请求/响应 JSON 数据结构
  validate.go                    输入校验
  simulator.go                   离散事件模拟器 + Chandy-Lamport 协议
  simulator_test.go              自动化测试
examples/                        请求样例（含 out/ 下的真实运行结果）
run_examples.sh                  批量运行样例
```

## 构建与运行

```bash
go build -o bin/snapshotsim ./cmd/snapshotsim

# 文件方式
./bin/snapshotsim -in examples/01-empty-channels.json -out /tmp/result.json

# 标准输入方式
cat examples/02-inflight-capture.json | ./bin/snapshotsim

# 运行全部样例（结果写入 examples/out/）
./run_examples.sh

# 自动化测试（含竞态检查）
go test -race -v ./...
```

要求 Go 1.22+，无第三方依赖。

## 模型

### 节点与转账

- 每个节点有一个整数余额。`transfer` 事件在发送 tick **立即扣款**，
  应用消息经信道延迟后在接收 tick **上账**。
- 余额不足的转账被拒绝（事件日志中记录 `rejected`），不产生消息。
  因此在可靠 FIFO 网络中，**系统总钱数在任何时刻守恒**：

  ```
  全部节点余额之和 + 全部在途消息金额之和 = 初始总钱数
  ```

- 守恒是 Chandy-Lamport 快照的验收依据：对每个完成的快照，
  `states_sum + in_flight_sum` 必须等于初始总钱数。

### 信道

| policy | 语义 |
|---|---|
| `fifo`（缺省） | 可靠、保序：消息按发送顺序在 `base_delay` 个 tick 后投递 |
| `best_effort` | 可配置丢包率、重复率、随机抖动、乱序（消息可能提前到达） |

Chandy–Lamport 算法只在 `fifo` 链路上成立。`best_effort` 链路是对照实验：
标记（marker）丢失时快照永远无法完成，结果如实输出 `complete: false`。

### Chandy–Lamport 协议（本实现）

1. 节点 N 发起快照 `id`：**记录本地状态**（当前余额），沿每条出信道发送标记。
2. 节点第一次收到快照 `id` 的标记：记录本地状态；沿自己的出信道继续传播标记；
   此后开始在"发送该标记的入信道"之外的每条入信道上记录在途应用消息。
   发送该标记的那条入信道立即**关闭**（标记之前的消息已全部到达）。
3. 某条入信道的标记到达：关闭该信道的在途记录。
4. 当所有节点都已记录状态、所有入信道都已关闭：快照完成，汇总
   `states`（各节点记录时刻余额）与 `in_flight`（各信道记录到的消息金额）。

交叠保证：每个 `(节点, snapshot_id)` 持有独立的 `localSnap`；
一条应用消息到达时，逐快照独立判断"本节点已记录且该入信道未关闭"才计入，
因此 s1 的信道关闭不会影响 s2 的记录。

### 确定性与事件顺序

- 单一全局事件堆，按 `(tick, 全局序号)` 排序。
- 同一 tick：脚本事件先展开入堆（按 JSON 中出现顺序），运行时产生的投递事件
  随后按产生顺序入堆，因此同 tick 顺序稳定可复现。
- FIFO 信道固定延迟 + 同 tick 全局序号，保证同信道消息严格按发送顺序投递。
- 所有随机数来自以 `seed` 初始化的局部 `rand.Rand`；相同输入两次运行结果逐字节一致。

## 请求 JSON

```json
{
  "seed": 7,
  "tick_limit": 60,
  "nodes": [{"name": "A", "balance": 120}],
  "links": [
    {"from": "A", "to": "B", "policy": "fifo", "base_delay": 1}
  ],
  "events": [
    {"kind": "transfer", "tick": 0, "from": "A", "to": "B", "amount": 2,
     "repeat_to": 40, "repeat_every": 2},
    {"kind": "marker", "tick": 5, "from": "A", "snapshot_id": "k1"}
  ]
}
```

| 字段 | 说明 |
|---|---|
| `seed` | 随机种子（best_effort 信道的全部随机行为） |
| `tick_limit` | 逻辑时间上界；到点未处理的事件不再执行（缺省 = 最后事件 tick + 100） |
| `nodes[].balance` | 初始余额，非负 |
| `links[].policy` | `fifo`（缺省）或 `best_effort` |
| `links[].base_delay` | 投递基准延迟（tick），缺省 1 |
| `links[].jitter` | best_effort：附加随机延迟 `[0, jitter]` |
| `links[].loss_pct` / `dup_pct` | best_effort：丢包/重复概率百分比 `[0,100]` |
| `links[].reorder` | best_effort：允许消息以概率提前到达制造乱序 |
| `events[].kind` | `transfer`（需 from/to/amount>0）或 `marker`（需 from/snapshot_id） |
| `repeat_to` / `repeat_every` | 周期事件：从 tick 到 repeat_to 每 N tick 重复（N 缺省 1） |

## 响应 JSON（节选）

```json
{
  "seed": 7,
  "last_tick": 42,
  "final_balances": {"A": 120, "B": 80},
  "snapshots": [
    {
      "id": "k1",
      "complete": true,
      "initiator": "A",
      "states": {"A": 118, "B": 80},
      "in_flight": [{"from": "B", "to": "A", "count": 1, "sum": 2, "amounts": [2]}],
      "states_sum": 198,
      "in_flight_sum": 2,
      "total": 200
    }
  ],
  "channel_stats": [{"from": "A", "to": "B", "policy": "fifo",
                     "sent": 23, "delivered": 23, "lost": 0, "duplicated": 0, "reordered": 0}],
  "log": [{"tick": 5, "kind": "snapshot", "from": "A", "snapshot_id": "k1", "detail": "..."}]
}
```

未完成快照额外包含 `"complete": false` 与 `missing_channels`
（形如 `"state:B"`、`"A->B"`，说明哪些节点状态/信道标记缺失）。

## 验收场景与样例

| 样例 | 场景 | 期望 |
|---|---|---|
| `01-empty-channels` | 空闲系统发起快照 | 在途为空，total = 150 |
| `02-inflight-capture` | 转账在慢链路上时发起快照 | A=70、B=50，捕获 A->B 在途 30，total = 200 |
| `03-overlap-continuous` | 双向周期转账流中两个交叠快照 | k1、k2 各自 total = 200，互不串台 |
| `04-marker-interleave` | 两快照标记在同 FIFO 信道交错 | s1 不收同 tick 的 20 元转账、s2 收，各自 total = 300 |
| `05-best-effort-faults` | 丢包/重复/乱序对照 | 统计如实；守恒被网络故障破坏 |
| `06-marker-lost-incomplete` | 标记 100% 丢失 | `complete=false`，列出缺失状态与信道 |

自动化测试位于 `internal/simulator/simulator_test.go`，除上表场景外还覆盖：
确定性复现（同输入两次结果逐字节一致）、输入校验（7 类非法输入）、
丢包/重复/乱序的统计断言（乱序在 200 个确定性 seed 上验证必然出现）。

## 限制与设计取舍

- 单进程、单线程事件循环；不存在真正的并发，所有交错由事件顺序显式构造。
- 转账金额为整数，发送方余额不足即拒绝（保证守恒模型简单可验证）。
- 不提供 HTTP 服务与前端；接口就是 JSON 文件/stdin-stdout。
- best_effort 下重复消息会被接收方重复上账、丢失消息不上账——这是对真实网络
  故障的忠实模拟，不是 Chandy-Lamport 的有效运行环境。
