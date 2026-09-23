# orset — Observed-Remove Set 确定性离散事件模拟器

纯后端项目（Go，仅标准库）：在**单进程内**模拟多个副本上的
Observed-Remove Set（OR-Set，状态型 CRDT）。网络可以丢包、重复和乱序，
行为完全由场景 JSON 与随机种子决定，可逐字节复放。**不监听端口、不依赖
真实时钟或集群设施。**

## 1. 数据模型与正确性论证

### 1.1 OR-Set 定义

每个副本维护两个映射：

```
A: value -> set<unique-tag>   // 添加标签集（observed adds）
R: value -> set<unique-tag>   // 墓碑标签集（observed removes）
```

- 有效元素：`value ∈ S` 当且仅当 `A[value] \ R[value] ≠ ∅`。
- **Add**：分配一个全局唯一标签 `t = (origin, counter)`，加入 `A[value]`。
- **Remove（观察删除）**：只把当前**已观察到的存活标签**
  `A[value] \ R[value]` 拷入 `R[value]`。它对尚未到达的并发 Add 标签
  一无所知，因此无法删除它们——并发新增天然保留。
- **Merge（join）**：逐键集合求并：`A := A ∪ A'`，`R := R ∪ R'`。

### 1.2 唯一标签的不可变约定

`(origin, counter)` 中 origin 是发起副本，counter 是该副本单调递增的本地
序号。标签一旦分配就**永不复用**：复用已存在于 `R` 中的标签会让新值“出生
即死亡”；复用 `A` 中标签会让两个值共享同一条添加事实。`AddWithTag`
对全部已观察历史 `A∪R` 做唯一性检查，违反即报错。

### 1.3 交换、结合、幂等

Merge 在标签集合上就是集合并集。对任意副本状态 `s, a, b`：

- 交换律：`s ⊔ a = a ⊔ s`
- 结合律：`(s ⊔ a) ⊔ b = s ⊔ (a ⊔ b)`
- 幂等律：`s ⊔ s = s`

因此：消息**乱序**到达只改变合并的中间态，不改变最终 join；消息**重复**
到达等价于重复并集，状态不变。模拟器中每条重复投递都会被记账，并报告
该次合并是否为“冗余合并”（接收者指纹未变化）。验收项 A1/A2/A5 对此做
随机与穷举两种验证。

### 1.4 墓碑回收（GC）的稳定性前提

`R` 只增不减，长期运行需要回收。直接删除墓碑是危险的：一个**尚未观察到
该墓碑**的滞后/分区副本仍持有对应 `A` 标签，它回归合并时会把该标签重新
当作存活的添加事实——元素**复活**。

安全回收必须同时满足：

1. 待回收的每个墓碑标签已被**所有现在或将来可能与本副本合并的副本**
   观察到（出现在它们的 `R` 中）。检查集合必须完整：遗漏一个长期分区或
   退役后又回归的副本，回收就不安全。代码见 `SafeToReclaim(others...)`。
2. 标签全局唯一、永不复用（由分配策略保证，非运行时可检查）。

`ReclaimGC()` 本身**不做前提检查**——是否满足前提是部署/协议层的判断；
调用方应先调用 `SafeToReclaim`。验收 A7 同时演示安全路径（回收后有效值
不变、对称回收的副本仍然收敛）和危险路径（遗漏滞后副本时元素复活）。

## 2. 离散事件模拟器

- 时间是整数 tick；事件存放在小顶堆中，按 `(at, seq)` 严格排序，
  同 tick 事件按场景声明顺序执行，保证调度确定。
- PRNG 使用 `math/rand/v2` 的 PCG，由 seed 确定（无全局随机源、
  无 wall clock、无 goroutine 间不确定性）。
- 网络模型（每条逻辑消息独立判定）：
  - `drop_prob`：整条丢弃；
  - `duplicate_prob`：再产生一份副本，两份副本独立抽取延迟
    （副本自身也可能造成乱序），副本不再递归判定丢包/重复；
  - `reorder_prob`：若该方向上仍有在途消息，把本消息的到达时刻提前到
    最早在途消息之前（不引入负延迟；不可行时不强制）。
    此外，各消息延迟独立均匀抽取也会**自然**产生后发先至。
    trace 中以每个 (from,to) 方向的发送序号是否回退来判定实际乱序。
  - `min_delay/max_delay`：整数 tick 均匀延迟区间。
- 传输的是**发送时刻的完整状态快照深拷贝**（state-based），接收方做一次
  Merge；发送后状态再变化不影响在途消息。
- 副本本地标签计数器只随本地 Add 递增，合并不动它，因此标签永无冲突。

## 3. JSON 运行接口

### 3.1 请求（场景）

```json
{
  "name": "示例",
  "seed": 42,
  "replicas": ["r1", "r2", "r3"],
  "network": {
    "drop_prob": 0.3, "duplicate_prob": 0.3,
    "reorder_prob": 0.3, "min_delay": 1, "max_delay": 5
  },
  "events": [
    {"at": 1, "kind": "add",    "replica": "r1", "value": "x"},
    {"at": 2, "kind": "remove", "replica": "r1", "value": "x"},
    {"at": 3, "kind": "sync",   "replica": "r1", "to": "r2"},
    {"at": 4, "kind": "gossip", "replica": "r1"}
  ],
  "expected_values": ["x"]
}
```

事件类型：

| kind   | 含义                                                                 |
|--------|----------------------------------------------------------------------|
| add    | 在 replica 本地添加 value，标签 `replica#本地序号`                   |
| remove | 在 replica 本地对 value 执行观察删除（只墓碑化已观察标签）           |
| sync   | replica → to 定向推送一次完整状态                                    |
| gossip | replica 向其余所有副本各推送一次完整状态（全连接扇出）               |

`expected_values` 可选；给出后响应中包含 `matches_expected`
（要求状态级收敛且有效值等于期望）。

### 3.2 响应

顶层字段：`converged`（所有副本 A/R 完整状态相同）、
`values_converged`（仅有效值相同）、`matches_expected`、
`stats`（add/remove 次数、发送/丢弃/重复/投递计数、冗余合并计数、
实际乱序次数、最终 tick）、`nodes`（每个副本的有效值与 A/R 标签）、
`trace`（每个 add/remove/send/deliver 动作及其后的有效值）。
样例响应见 `examples/output/*.result.json`。

## 4. 使用方法

```bash
go build -o orset .

# 运行一个场景（结果 JSON 写标准输出）
./orset run examples/01-basic.json

# 写入文件
./orset run -o /tmp/out.json examples/02-unreliable.json

# 内置验收枚举（全部通过退出码 0，任一不过退出码 1）
./orset accept
./orset accept -o examples/output/accept-report.json

# 自动化测试
go test -race -count=1 ./...
```

注意 Go 标准库 flag 要求 `-o` 写在位置参数**之前**。

## 5. 验收项对照

| 编号 | 验收要求                                   | 实现位置                          |
|------|--------------------------------------------|-----------------------------------|
| A1   | 合并交换/结合/幂等（300 轮随机状态）       | `accept/accept.go` mergeLaws      |
| A2   | 三副本 add/remove 快照 6!=720 全排列乱序合并枚举 | threeReplicaPermutations    |
| A3   | 乱序+重复网络下并发新增保留（100 种子）    | concurrentAddsRetained            |
| A4   | 观察删除只移除已观察标签（100 种子+单元断言）| concurrentAddSurvivesObservedRemove |
| A5   | 100% 重复投递下重复同步收敛（50 种子）     | duplicateSyncConverges            |
| A6a  | 40% 丢包、15 轮 gossip 最终收敛（30 种子） | dropStressConverges               |
| A6b  | 丢包确实造成分叉、补传后重新收敛           | dropActuallyDiverges              |
| A7   | 墓碑回收稳定性前提（安全路径+复活反例）    | tombstoneGCStability              |
| A8   | 确定性复放（同 seed 输出逐字节一致）       | determinismReplay                 |

A2 额外枚举了一组六张快照的 720 种投递顺序（合计 726 种），
每种顺序都逐状态（而非仅有效值）与基准 join 比对。

## 6. 目录结构

```
crdt/orset.go          OR-Set 状态、Add/观察删除/Merge/GC
crdt/*_test.go         CRDT 单元测试
sim/scenario.go        场景类型、JSON 加载与校验
sim/engine.go          确定性离散事件引擎、trace 与结果
sim/engine_test.go     模拟器测试（丢包/重复/乱序/确定性/校验）
accept/accept.go       九项验收枚举
accept/accept_test.go  将验收接入 go test
main.go                CLI：run / accept
examples/              请求样例
examples/output/       实际运行产生的响应样例与验收报告
RUNLOG.md              实际运行命令与结果记录
```

## 7. 边界与非目标

- 不做网络服务、HTTP 端口、前端；JSON 接口走文件/标准输入输出。
- 模拟器验证的是 CRDT 数学性质与协议收敛，不模拟真实节点故障、
  拜占庭行为或磁盘持久化。
- 状态快照随标签数线性增长；这是 OR-Set 的固有代价，靠 1.4 的安全 GC
  在协议层缓解。
