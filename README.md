# causal-broadcast — 因果广播缓冲模拟器（纯后端）

单进程内的**确定性离散事件模拟器**：多个节点在不可靠网络上做带向量依赖的
因果广播；网络可丢包、重复、乱序；依赖未满足的先进缓冲，重复消息只投递一次，
缓冲满时返回背压且不破坏因果顺序。**不依赖任何真实集群、网络或 wall-clock
时间**——所有时序都是虚拟时钟，同一份 JSON 请求（含 seed）永远产出逐字节
相同的结果。

## 为什么用受限拓扑才能"真正丢包"

默认是全互联 gossip，任意一跳丢失都会被其他持有节点的后续转发自然修复。
为了让故障注入能表现出**不可自愈的丢失/永久缺失**，请求支持 `topology`
有向邻接表；只有沿邻接边的复制会被发送，链路上的 `drop` 故障因此能造成
真实不可达。

## 目录结构

```
go.mod
cmd/cbsim/main.go                 # JSON 运行接口（CLI：-in/-out，也支持 stdin/stdout）
internal/
  vec/clock.go                    # 向量钟、(node,seq) 键、依赖满足判定
  sim/scheduler.go                # 确定性离散事件调度器（(时间, FIFO) 小顶堆）
  simnet/network.go               # 不可靠网络：丢包/重复/延迟 + 显式故障注入
  node/node.go                    # 因果投递、hold-back 缓冲、去重、背压
  engine/engine.go                # 编排：拓扑、广播/触发器/重传、最终诊断
examples/                         # 5 个验收请求样例
examples/out/                     # 上述样例的实际运行结果（已提交）
RUNLOG.md                         # 真实执行的命令、结果与未通过项记录
```

## 构建与运行

```bash
go build -o bin/cbsim ./cmd/cbsim

# 文件方式
./bin/cbsim -in examples/01-chain-buffer-release.json -out /tmp/01.json

# 管道方式
cat examples/03-loss-repair.json | ./bin/cbsim > /tmp/03.json
```

退出码：`0` 正常（含仿真级校验错误，会以 `"status":"error"` 返回）；
`1` JSON 无法解析 / 文件错误；`2` 请求校验失败。

## 请求 JSON 字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `seed` | int | 随机网络的种子（同 seed 可复现） |
| `nodes` | string[] | 节点 id，至少 2 个 |
| `topology` | map(node→neighbors[]) | 可选，有向邻接表；缺省=全互联。给出时必须列出所有节点 |
| `end_time_ms` | int | 虚拟仿真窗口；`t <= end_time_ms` 的事件都会执行 |
| `buffer_capacity` | int | hold-back 缓冲容量；`0`=默认 16，`-1`=无限 |
| `network` | object | `loss_rate`、`duplicate_rate` ∈ [0,1]，`min_delay_ms`/`max_delay_ms` |
| `faults` | object[] | 确定性故障，逐条匹配（先匹配先生效） |
| `broadcasts` | object[] | `{at_ms, node, payload}` 定时广播 |
| `triggers` | object[] | `{when_node, after_origin:{node,seq}, payload}`：节点投递某消息后链式广播 |
| `retransmits` | object[] | `{at_ms, from, from_origin, to[], delay_ms}`：从持有节点直接注入修复副本 |
| `backpressure_retry` | object | `{delay_ms, max_attempts}`：`0`=放弃（默认），`-1`=无限重试 |

故障匹配键：`origin_node` / `origin_seq` / `from` / `to`，留空为通配；
**注意：省略 `from` 时锚定为"源头直发"（from=origin）**，要命中中继转发副本
需显式写 `from`。`action` ∈ `deliver|drop|duplicate|delay`。

## 响应 JSON 关键字段

- `deliveries[]`：应用层投递记录（含投递时虚拟时间、节点、来源、投递后向量钟）。
- `duplicates_suppressed_per_node`：每个节点丢弃的重复副本数。
- `backpressure_events[]`：缓冲满时拒绝的副本及其 `waiting_on` 依赖。
- `retry_events[]`：背压重试生命周期（`delivered|buffered|canceled|given_up`）。
- `network_decisions[]`：每条复制副本网络做了什么（`delivered|duplicate|dropped`，
  `fault` 或 `random`）。
- `final_report`：
  - `buffered_per_node` / `missing_for_buffered`：窗口结束仍在缓冲、各缺哪些前驱；
  - `undelivered_origins`：至少一个节点未投递的消息；
  - `root_missing_blockers`：**只有源头节点持有的消息**——无接收方能修复；
  - `diagnostics`：人类可读的永久缺失/阻塞诊断。

## 因果投递规则

- 消息携带依赖向量：源节点广播时已投递的全部 `(node, seq)`。
- 节点维护"已投递向量钟" V。消息 m（源 O、序号 s）可投递当且仅当：
  1. 对每个非自身源的依赖 k 有 `V[k]` 满足；2. `V[O] >= s-1`（同源前驱）。
- 不满足则进入 hold-back 缓冲；每当新消息投递，递归释放变就绪的缓冲消息。
- 已投递或已缓冲的来源再次到达 → 计为重复并丢弃（恰好投递一次）。
- 缓冲满且消息未就绪 → 背压拒绝，**不修改任何状态**，可安全重试。

## 五个验收场景

| 样例 | 验证点 |
|---|---|
| `01-chain-buffer-release` | 链式广播（触发器生成）；n3 先收到子消息，前驱延迟 30ms；前驱到齐后同一时刻按因果序释放 |
| `02-concurrent-reorder-dup` | 并发广播；n3 先收 n2:1（2ms）后收 n1:1（20ms）=乱序；n4 收到重复副本且只投递一次 |
| `03-loss-repair` | 前驱首跳全部 drop；40ms 由源重传，42ms 两节点补齐释放 |
| `04-backpressure-retry` | 容量=1；后继先到触发背压，无限重试；前驱 40ms 到达后按序冲刷，且不违反因果 |
| `05-permanent-loss-diagnosis` | n1:1 对 n3 超过窗口（500ms），n1:2 滞留缓冲；n1:3 对所有接收方丢失 → 报根阻塞与等待关系 |

## 测试

```bash
go test ./...          # 单元 + 端到端验收
go test -race ./...    # 竞态检测（本项目无 goroutine，应干净通过）
go vet ./...
```

测试覆盖：向量钟语义、缓冲释放、去重恰好一次、背压不破坏因果、跨源依赖、
调度器 FIFO、五个验收场景、确定性复现（逐字段 DeepEqual）、请求校验错误。

## 明确的边界（非目标）

- 无前端、无网络服务监听；只有 CLI/库 + JSON。
- 无真实节点、socket、集群；一切在单进程内用虚拟时间模拟。
- 不实现真实成员管理/崩溃恢复；"丢失"指仿真窗口内不可达，重传由请求显式驱动。
