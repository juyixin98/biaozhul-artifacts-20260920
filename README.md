# Observed-Remove Set 确定性离散事件模拟器（纯后端）

Go 实现的 **OR-Set（Observed-Remove Set，CRDT）** 及其**单进程确定性离散事件模拟器**，带 JSON 运行接口（CLI 与 HTTP）。节点全部在同一进程内模拟，**不依赖任何真实集群、真实网络或系统时钟**；模拟网络可独立配置**丢包、重复、乱序**。

- 语言：Go 1.23，仅用标准库
- 无前端、无第三方依赖

## 1. OR-Set 语义

每个副本维护两份状态：

```
Adds    : element → set(tag)   // 见过的全部"添加标签"（含已删的）
Removed : set(tag)            // 已观察到的删除（墓碑）
```

- **添加**：铸造一个全局唯一标签 `tag = (副本ID, 该副本单调递增序号)` 绑定到元素。无需协调即可保证唯一，且序号是计数器而不是随机数，保证模拟可复现。
- **删除（observed-remove）**：只把删除时刻**本副本已经观察到**的该元素标签写入墓碑集。尚未观察到的并发添加标签不受影响。
- **查询**：元素存在 ⇔ 至少有一个绑定标签"在 Adds 中且不在 Removed 中"。
- **合并（merge）**：两个分量都做普通**集合并**：

  ```
  Adds'    = Adds₁ ∪ Adds₂
  Removed' = Removed₁ ∪ Removed₂
  ```

  集合并天然满足**交换律、结合律、幂等律**，所以合并顺序、重复投递、乱序投递都不影响最终状态。

### 并发新增为何保留

```
A: add x → tag A:1
B: add x → tag B:1        （与 A 的添加并发，B 从没见过 A:1）
A: remove x               （A 只观察到 A:1，只给 A:1 立墓碑）
merge(A,B): x 仍在，因为 B:1 存活
```
删除一个你从未见过的标签在语义上是不可能的——这就是 "observed-remove"。删除后再添加会铸造**新**标签，因此"删了又加"正确生效。

### 墓碑回收（GC）的稳定性前提

墓碑不能随便删。标签 `t` 及其墓碑只有在满足下列**稳定性前提**时才可回收：

1. 每一个**今后还可能把状态合并回来**的副本（包括当前分区、停机、可能拿旧快照回归的副本）都已经观察到标签 `t`；并且
2. 这些副本**全部**已经观察到 `t` 的墓碑。

之后还要求回收动作本身基于**回收前快照**一次性对所有副本做决策（原子屏障），避免"先回收的节点墓碑消失，导致后回收的节点误判前提不成立"。若无法确认成员集合完整（如有失联副本），**必须不回收**，否则掉队者带着"知标签、不知墓碑"的旧状态回归时会让元素复活。

本项目提供：
- `orset.StableTags(element, replicas...)`：求所有给定副本都观察到的标签；
- `(*State).Reclaim(tags, witnesses...)`：仅当标签在本地有墓碑、且**每个见证者**都有墓碑时才删除，否则原样保留；
- 模拟器的 `gc` 事件：用回收前快照对全部配置节点执行一次协调回收。

## 2. 目录结构

```
.
├── go.mod
├── cmd/orset/main.go            # CLI：run（读 JSON 配置出报告）/ serve（HTTP JSON）
├── internal/
│   ├── orset/                   # OR-Set CRDT：状态、增删、合并、回收、JSON 编解码
│   │   ├── orset.go
│   │   ├── json.go
│   │   ├── parse.go
│   │   ├── orset_test.go
│   │   └── acceptance_test.go   # 验收枚举测试
│   └── sim/                     # 离散事件模拟器（事件堆、网络模型、trace、报告）
│       ├── sim.go
│       └── sim_test.go
├── examples/                    # 请求样例
│   ├── 01-three-replica-add-delete.json
│   ├── 02-concurrent-add-out-of-order.json
│   ├── 03-lossy-repeated-sync.json
│   ├── 04-tombstone-gc.json
│   └── output/                  # 上述样例的实际报告与 trace（已运行生成）
├── README.md
└── RUNLOG.md                    # 实际执行命令、结果、中途失败与修复的如实记录
```

## 3. 构建与测试

```bash
# 需要 Go 1.23+
go vet ./...
go build ./...
go test ./...            # 全部自动化测试
go test -race ./...      # 可选：竞态检测
go build -o bin/orset ./cmd/orset
```

## 4. JSON 运行接口

### 4.1 配置格式（请求）

```json
{
  "seed": 42,
  "nodes": ["n1", "n2", "n3"],
  "network": {
    "loss_prob": 0.35,
    "duplicate_prob": 0.30,
    "reorder_prob": 0.60,
    "min_delay": 1,
    "max_delay": 3
  },
  "events": [
    {"time": 1, "node": "n1", "op": "add", "element": "a"},
    {"time": 2, "node": "n1", "op": "sync"},
    {"time": 5, "node": "n2", "op": "remove", "element": "a"},
    {"time": 6, "node": "n2", "op": "sync", "target": "n1"},
    {"time": 30, "node": "n1", "op": "gc"}
  ],
  "max_ticks": 0
}
```

字段说明：

| 字段 | 含义 |
|---|---|
| `seed` | 随机源种子。同种子 + 同配置 ⇒ 逐字节相同的运行结果（确定性） |
| `nodes` | 副本名列表（唯一、非空） |
| `network.loss_prob` | 每条消息被丢弃的概率，范围 `[0,1)` |
| `network.duplicate_prob` | 未丢弃的消息额外再投递一份的概率 |
| `network.reorder_prob` | 消息被刻意滞留（额外加一个 max_delay）以制造可观察乱序的概率 |
| `network.min_delay` / `max_delay` | 基础投递延迟（抽象整数 tick，均匀抽取） |
| `events[].op` | `add` / `remove`（需 `element`）、`sync`（`target` 缺省=向所有其他副本广播）、`gc` |
| `max_ticks` | 可选，超过该 tick 的网络事件不再处理；0 = 运行到队列清空 |

随机判定按固定次序进行（丢包→重复→乱序→延迟），保证可复现。同一 tick 内多个客户端事件按配置中的顺序依次发生。

### 4.2 CLI

```bash
# 报告写到 stdout（-o 指定文件），--trace 把人类可读事件轨迹打到 stderr
./bin/orset run examples/03-lossy-repeated-sync.json -o report.json --trace
# flag 在文件名前后均可
```

### 4.3 HTTP

```bash
./bin/orset serve :8080
curl -s -X POST localhost:8080/simulate \
  -H 'Content-Type: application/json' \
  --data @examples/02-concurrent-add-out-of-order.json
# GET /healthz -> ok
```

### 4.4 报告格式（响应）

```json
{
  "seed": 2024,
  "converged": true,
  "all_states_equal": true,
  "final_values": ["a", "b", "c"],
  "ticks": 87,
  "counts": {"add":3, "send":30, "drop":14, "duplicate":6,
             "reorder":11, "deliver":22},
  "nodes": [
    {"node":"n1", "values":["a","b","c"],
     "state":{"adds":{"a":["n1:1"], ...}, "removed":[]}}
  ],
  "trace": [ {"time":1,"kind":"add", ...}, {"time":2,"kind":"send", ...},
             {"time":4,"kind":"drop", ...}, {"time":7,"kind":"deliver", ...} ]
}
```

- `converged` / `all_states_equal`：运行结束（队列清空或达 max_ticks）时所有副本状态是否逐结构相等。**消息全丢且不再同步时为 `false`，这是如实报告而非错误。**
- `state` 即 OR-Set 的线路格式：标签为 `"origin:seq"`，墓碑列表排序输出。
- `trace` 逐条记录 add / remove / send / drop / dup / reorder / deliver / gc，含当时本地值集合，便于审计。

## 5. 模拟器如何工作

- 单个 `math/rand.Rand(seed)`、一个按 `(时间, 序号)` 排序的事件最小堆；没有 goroutine 充当节点、没有 wall clock。
- `sync` 对发送者当前状态做**深拷贝快照**投入网络，接收时执行 `Merge`。重复投递被幂等吸收；旧快照晚到不能复活已删元素（墓碑是并集）。
- 收敛依赖"最终有足够多的同步重传穿过丢包"。样例与测试通过多轮、彼此间隔大于最大延迟的全连接 gossip 来制造这个条件；这是使用方在编排输入时要提供的前提。

## 6. 验收点与对应测试

| 验收要求 | 测试 / 样例 |
|---|---|
| 枚举三副本添加删除与乱序合并 | `TestAcceptance_ThreeReplicaEnumeration`（6 种合并全序、双向 gossip 调度、新旧快照两种到达序）；`TestThreeReplicaMergeOrderEnumeration`；`TestOutOfOrderMergeEnumeration`；样例 01 |
| 并发新增保留 | `TestObservedRemoveKeepsConcurrentAdd`、`TestSimConcurrentAddPreserved`；样例 02（90% 乱序，最终 `{x}`） |
| 重复同步收敛 | `TestRepeatedSyncIsIdempotent`（同源合并 100 次）、`TestDuplicateDeliveryIdempotent`、`TestLossyReorderedRepeatedSyncConverges`；样例 03 |
| 交换/结合/幂等 | `TestMergeAlgebraRandomized`（200 组随机状态） |
| 墓碑回收稳定性前提 | `TestReclaimStability`（前提不满足必须拒绝）、`TestSimCoordinatedGC`；样例 04 |
| 确定性 | `TestDeterministicReplay`；同 seed 两次报告 `diff` 为空 |
| 坏网络（丢包/重复/乱序） | 上述 sim 测试 + 25 场景随机 fuzz `TestFuzzConvergence` |

实际命令与运行结果（含开发过程中出现过的失败与修复、未通过项说明）见 **`RUNLOG.md`**；样例真实输出见 `examples/output/`。

## 7. 快速试一次

```bash
go build -o bin/orset ./cmd/orset
go test ./...
./bin/orset run examples/02-concurrent-add-out-of-order.json --trace
```
