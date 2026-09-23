# 向量时钟冲突检测（vcconflict）

纯后端、单进程的**确定性离散事件模拟器**：在 Go 进程内模拟多个复制节点上的
版本化寄存器（versioned register），用**向量时钟**判定事件先后 / 并发，
通过 JSON 文件驱动并输出完整 JSON 轨迹。网络层可注入**丢包、重复、乱序**，
不依赖任何真实集群、协程网络或系统时钟。

核心安全性质：**并发版本永远不会被静默丢失**。寄存器始终保留全部并发兄弟
版本（siblings）；客户端合并写入必须携带它所覆盖的**完整上下文**（当前存活
兄弟版本的全集），上下文缺失或引用未知版本时写入被明确拒绝，状态不变。

## 目录结构

```
cmd/vcconflict/          CLI 入口（读 JSON 场景，输出 JSON 结果）
internal/vclock/         向量时钟：偏序比较 before/after/equal/concurrent、join
internal/register/       版本化寄存器：兄弟保留、完整上下文写入、对称合并
internal/sim/            确定性离散事件模拟器 + JSON 请求/响应结构
examples/                请求样例（6 个场景）
examples/output/         部分场景的完整 JSON 输出样例
```

## 快速开始

```bash
make build                       # 产出 bin/vcconflict
make test                        # 全部自动化测试（含 -race）
make demo                        # 依次运行 examples/ 下全部场景
bin/vcconflict -f examples/01-offline-writes.json
cat examples/02-duplicate-delivery.json | bin/vcconflict
```

Go 版本要求：1.22+（无第三方依赖，仅标准库）。

## 请求格式（场景 JSON）

| 字段 | 说明 |
|---|---|
| `name` | 场景名称 |
| `nodes` | 参与节点 ID 列表（单进程内模拟） |
| `seed` | 随机种子；相同种子 + 相同场景 → 字节级一致输出 |
| `network` | `drop_prob` / `duplicate_prob` ∈ [0,1]；`min_delay`/`max_delay` 逻辑拍数 |
| `events` | 离散事件列表 |

事件类型：

- `write`：`node`/`key`/`value`（任意合法 JSON）/`context`（覆盖的版本 ID 全集）
- `read`：读取该节点该 key 的存活兄弟
- `send`：从 `node` 发整份快照到 `to`；可逐事件覆盖 `drop_prob`/`duplicate_prob`/`delay`
- `broadcast`：发送给除自己外的全部节点

故障注入是**发送时**按固定顺序（丢包 → 重复 → 延迟）调用种子 RNG 完成的，
因此完全可复现。`delay` 以逻辑拍为单位，先发慢、后发快即制造乱序。

## 响应格式（结果 JSON）

- `stats`：接受/拒绝写、读、发送、丢包、重复、投递计数
- `trace`：严格有序的事件轨迹（`write` / `write_rejected` / `read` / `send` /
  `drop` / `duplicate` / `deliver`），投递项带每个 key 的合并摘要
  （`added` / `duplicate` / `pruned` / `pruned_incoming`）
- `final_state`：每个节点每个 key 的存活兄弟版本（含向量时钟）

## 关键语义

### 1. 向量时钟判定

对时钟 a、b：`a ≤ b` 当且仅当每个节点分量都不大于 b。据此分为
`equal` / `before`（a 先于 b）/ `after` / `concurrent`（互不为祖先）。

### 2. 兄弟版本保留

寄存器只保留极大版本集合；网络合并是**对称的并集 + 支配剪枝**，
天然满足交换律与幂等律——重复投递（`duplicate`）和乱序投递
（迟到的旧版本 `pruned_incoming`）都不会改变最终状态。

### 3. 完整上下文写入规则

客户端写必须在 `context` 中显式列出写目标节点上该 key **当前存活的全部**
兄弟版本 ID：

| 情形 | 结果码 | 状态变化 |
|---|---|---|
| 上下文引用了本节点从未见过的版本 | `unknown_context` | 无 |
| 上下文漏掉任一存活兄弟（含空上下文盲写已存在的 key） | `stale_context` | 无 |
| 完整覆盖全部兄弟 | 接受，产生单一后继版本 | 兄弟被后继取代 |

新版本时钟 = 上下文与该节点已观测的全部存活时钟之 join，再递增本节点分量。
拒绝错误会点名将被覆盖的那个遗漏版本（`new_version` 字段）或缺失 ID
（`missing`），便于客户端重新读取、补全上下文后重试。

### 4. 确定性与同拍顺序

- 逻辑时间为整数拍；同一拍内**网络投递先于客户端写**处理（投递在该拍收敛），
  结果不依赖事件声明顺序。
- 追加更晚的事件不会改变早期事件的结果（前缀确定性，见
  `TestPrefixDeterminism`）。

## 场景样例与实测结果（Go 1.22.2 / linux amd64）

以下均为本仓库实际运行 `bin/vcconflict -f <场景>` 的输出统计，完整 JSON
轨迹见 `examples/output/`。

| 场景 | 验证点 | 实测统计与结论 |
|---|---|---|
| `01-offline-writes` | 离线分叉、旧上下文拒绝、完整上下文合并 | 接受 3 / 拒绝 1（`stale_context`，点名 n1#1）；三节点最终收敛到单一后继 `n2#2` |
| `02-duplicate-delivery` | 100% 重复传输 | duplicates=2、deliveries=4；重复投递为幂等 no-op，n2 最终 `n1#2`，版本不膨胀 |
| `03-stale-context` | 盲写 / 部分上下文 / 未知上下文 / 合法合并 | 接受 3 / 拒绝 3（`stale_context`×2、`unknown_context`×1）；拒绝均不改状态，最终 `n2#2` |
| `04-reorder` | 显式 delay 制造乱序（新包先到） | 旧包迟到时 `pruned_incoming=[n1#1]`，最终仍为 `n1#2`，旧版本不复活 |
| `05-drop-then-repair` | 分区期丢包 → 反熵修复 | drops=2；分区期 n2 只见自己的 `n2#1`；修复后两节点都持有并发兄弟 `[n1#1,n2#1]` |
| `06-chaos-seeded` | 35% 丢包 + 25% 重复 + 随机延迟，4 节点 | drops=4、duplicates=4；一个盲写被正确拒绝；多轮无故障反熵后全部节点收敛到同一全局极大集合（多个种子下由测试验证） |

## 自动化测试

```bash
go test -race -count=1 ./...
```

- `internal/vclock`：偏序分类、逆关系、join
- `internal/register`：离线分叉合并、两类拒绝、空上下文规则、合并幂等/可交换、
  三向分叉、快照隔离、计数器单调、上下文乱序/重复
- `internal/sim`：六个示例场景断言、同拍投递优先、**前缀确定性**、
  种子敏感性、多种子（1/2/3/42/4242/9999）混沌反熵后收敛到 oracle 极大集合、
  非法场景校验、JSON 往返

## 边界与非目标

- 不做前端、不监听网络端口；节点为进程内对象，唯一 I/O 是 JSON 文件/标准流。
- 合并“成功”后语义为“该节点上此 key 收敛为单一后继”，其他节点通过 gossip
  收敛；后继版本在全网传播完成前仍可能与未同步分支并存，符合最终一致语义。
