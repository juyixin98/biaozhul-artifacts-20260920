# 带围栏租约锁（Fencing Lease Lock）确定性模拟器

纯后端项目，用 Go 实现的**确定性离散事件模拟器**，在单进程内模拟多个节点、
一个带围栏号（fencing token）的租约锁服务和一个受保护资源。网络可以
**丢包、重复、延迟和乱序**；节点可以被**暂停**（模拟长 GC / 进程冻结）。
不依赖任何真实集群、网络或系统时钟——时间完全由模拟器推进，所有随机性由
种子驱动，结果可复现。

要回答的核心问题只有一个：

> 当锁的旧持有者因为续约丢失或暂停超过租期而“以为自己还持有锁”时，
> 它迟到的写操作能否破坏受保护资源？

答案是**不能**——只要资源在写入时校验单调递增的围栏号。本仓库把三种故障
（续约丢失、暂停超过租期、消息迟到）逐一构造出来，并提供一个关闭围栏校验
的对照实验，证明缺少该机制时同样的故障会导致数据被旧持有者破坏。

---

## 1. 背景与模型

### 1.1 租约锁

客户端向锁服务请求资源，得到：

- 一个**围栏号 `fence`**：对该资源每次成功授予严格递增的整数（1, 2, 3, …）；
- 一个**租期 `ttl`**：在绝对模拟时刻 `expiry` 前有效；
- 客户端以 `ttl/2` 为间隔发送续约，服务端只接受“当前持有者 + 正确围栏号 +
  未过期”的续约。

锁服务对每个请求按 `(method, req_id)` 做**幂等去重**，网络层把请求复制两份
也不会产生两个围栏号。

### 1.2 围栏（核心）

受保护资源不查询锁服务，也不感知时间，它只记住一件事：

```
high_water = 见过并接受的最大围栏号
写入被接受  <=>  携带的 fence > high_water
```

于是即使旧持有者的客户端内存里还留着 `fence=1` 并且继续运行，它的写请求在
资源处必然 `fence (1) <= high_water (2)` 而被拒绝。**正确性不依赖客户端
自觉，而由资源端强制保证。**

### 1.3 三种故障如何被构造

| 故障 | 场景文件 | 构造手段 |
|---|---|---|
| 续约丢失/迟到 | `examples/renew-lost.json` | 网络规则把第 1 次续约延迟 12 tick，租期 15，续约在过期后才到达被拒 |
| 暂停超过租期 | `examples/pause-expired.json` | `t=4` 暂停 A 到 `t=22`，租期 10；期间 B 取得 `fence=2` 并写入 |
| 消息迟到 | `examples/late-message.json` | 续约消息在网络中滞留，与 A 迟到的提交一起在 B 接管后到达 |
| 消息重复（附加） | `examples/duplicate.json` | 网络复制续约请求，验证服务端幂等 |
| 无围栏对照（附加） | `examples/no-fence.json` | 资源关闭围栏校验，旧持有者写入**被接受**，顺序被破坏 |
| 随机压力（附加） | `examples/fuzz.json` | 15% 丢包 + 8% 重复 + 8% 乱序 + 暂停，多种子不变量验证 |

### 1.4 节点暂停语义

调用 `Pause(node, until)` 后，所有目的地为该节点的事件（入站消息、它自己的
定时器）都被缓存在一个按到达顺序排列的队列里，世界其余部分继续推进；到
`until`（或显式 resume）时，缓存事件在恢复时刻一次性刷入。这精确模拟了
“进程冻结期间租约到期、别人接管，解冻后旧进程继续执行”的情形：它内存中的
围栏号不会变旧，但世界已经变了。

---

## 2. 目录结构

```
go.mod
cmd/fls/                 CLI：run / demo / demo-json / list
internal/
  sim/                   离散事件引擎：事件堆、确定性 RNG、故障网络、节点暂停
    engine.go            时钟、事件队列、暂停/恢复
    network.go           显式规则（drop/delay/duplicate/nth）+ 随机背景行为
    rng.go               确定性 PCG 随机源
  proto/                 锁与资源 RPC 的消息结构（JSON body）
  lock/                  带围栏号的租约锁服务（授予/续约/释放/幂等去重）
  resource/              受保护资源（high-water 围栏校验）
  node/                  客户端节点（心跳续约、本地截止、暂停后僵尸写）
  runner/                JSON 场景装载、调度、断言与内置不变量检查
examples/                请求样例（场景 JSON）与对应输出（examples/output/）
run_tests.sh             一键：vet + race 测试 + 全部场景
RUNLOG.md                实际运行记录（命令、结果、失败项）
```

---

## 3. 构建与运行

需要 Go 1.22+（本机为 1.22.2）。

```bash
go build -o bin/fls ./cmd/fls

# 运行一个 JSON 场景
./bin/fls run examples/pause-expired.json

# 结果写入文件，stdout 安静
./bin/fls run examples/fuzz.json --out result.json --quiet

# 内置场景
./bin/fls list
./bin/fls demo renew-lost
./bin/fls demo fuzz           # 10 个确定性种子的聚合结果

# 导出内置场景 JSON
./bin/fls demo-json pause-expired

# 一键全部测试
./run_tests.sh
go test -race ./...
```

退出码：全部断言与内置围栏单调性不变量通过为 `0`，否则为 `1`。
`no-fence` 对照实验**按设计以 1 退出**。

---

## 4. JSON 运行接口

场景即一个 JSON 文档（见 `examples/*.json`）：

```json
{
  "name": "pause-longer-than-lease",
  "seed": 1,
  "max_time": 60,
  "network": {
    "min_delay": 1, "max_delay": 0,
    "loss_prob": 0.0, "duplicate_prob": 0.0, "reorder_prob": 0.0,
    "rules": [
      { "name": "hold-renew", "action": "delay", "delay": 8,
        "match": { "from": "A", "to": "lockserver",
                   "method": "lock.renew", "nth": 1 } }
    ]
  },
  "resources": [ { "name": "acct", "node_id": "res-1", "fenced": true } ],
  "nodes": ["A", "B"],
  "actions": [
    { "time": 0,  "type": "acquire", "node": "A", "resource": "acct", "ttl": 10 },
    { "time": 4,  "type": "pause",   "node": "A", "until": 22 },
    { "time": 12, "type": "acquire", "node": "B", "resource": "acct", "ttl": 10 },
    { "time": 16, "type": "submit",  "node": "B", "resource": "acct", "value": "b" },
    { "time": 24, "type": "submit",  "node": "A", "resource": "acct", "value": "a-zombie" }
  ],
  "expect": [
    { "kind": "rejected", "resource": "acct", "node": "A",
      "field": "fence_too_old", "min": 1 },
    { "kind": "last_water", "resource": "acct", "equals": 2 }
  ]
}
```

### 4.1 `network`

- `min_delay` / `max_delay`：单跳基础延迟与均匀抖动；
- `loss_prob` / `duplicate_prob` / `reorder_prob`：随机背景故障概率（由 `seed`
  决定，可复现）；
- `rules`：**显式确定性故障**，优先于随机背景行为：
  - `action: "drop"`：丢弃；
  - `action: "delay"`，`delay`：推迟指定 tick（迟到消息）；
  - `action: "duplicate"`，`duplicate_delay`：额外投递一份副本；
  - `match.nth`：只对第 N 次匹配的发送生效（精确打击“第 1 次续约”）。
- 规则也可以在运行中通过 `add_rule` 动作注入。

### 4.2 `actions`

`acquire` / `submit` / `release`（节点命令）、`pause`（`until` 自动恢复）/
`resume`（提前恢复）、`add_rule`、`compete`（压力工作循环：按 `every` tick
持续提交 `steps` 次，丢锁后仍继续，用于冲击围栏校验）。

### 4.3 `expect` 断言

| kind | 含义 |
|---|---|
| `record_count` | 指定 `node`/`type` 的 trace 数量，`min`/`max`（0 为显式边界） |
| `records_exist` / `record_absent` | 存在 / 不存在某类 trace |
| `accepted` | 某资源（可选某节点）接受的写次数 |
| `rejected` | 拒绝次数，可按 `field`（reason）过滤，如 `fence_too_old` |
| `last_water` | 资源最终 high-water 围栏号，`equals` |
| `accepted_order` | 被接受写入的节点顺序 |
| `granted_fences` | 信息性：列出授予的围栏号 |

此外，runner 对每个 `fenced: true` 的资源**无条件**检查内置不变量
`fence_monotonic`：接受的围栏号必须严格递增。

### 4.4 输出

`Result`（stdout 或 `--out` 文件）含 `ok`、`checks`、`commits`（被接受的写）
和完整 `traces`（带模拟时间的事件流），便于审计每一步。

---

## 5. 验收证据（摘要）

以 `renew-lost` 为例（完整 trace 见 `examples/output/renew-lost.result.json`）：

```
t= 1 lockserver lock.granted        fence=1            # A 成为持有者
t= 7 res-1      resource.accepted   fence=1 high=1     # A 持锁期间正常写
t= 9 network    net.delay                              # 续约被滞留 12 tick
t=16 A          client.lock_lost    fence=1            # A 本地截止
t=17 lockserver lock.granted        fence=2            # B 取得新围栏号
t=21 lockserver lock.renew_rejected fence=1 reason=fencing_token_stale
t=21 res-1      resource.accepted   fence=2 high=2     # B 的写被接受
t=22 res-1      resource.rejected   fence=1 high=2 reason=fence_too_old  # A 的僵尸写被拒
```

`pause-expired` 与 `late-message` 的关键结论相同：**资源提交从不接受较小围栏
号**。而在 `no-fence` 对照中，A 的旧写（fence=1）在 B（fence=2）之后被接受，
`fence_monotonic` 不变量报失败——直观说明没有围栏会发生什么。

随机压力 `demo fuzz`（10 种子）与 `TestFuzzManySeeds`（25 种子）在丢包、
重复、乱序、暂停组合下，均未出现围栏回退。

---

## 6. 设计说明与边界

- **单进程、单 goroutine 语义**：所有节点是事件队列上的回调，没有真实网络、
  没有 `time.Now()`、没有休眠；`go test -race` 通过。
- **锁服务与资源本身不暂停**：只有客户端节点暂停，模拟应用进程 GC；这与
  “锁/存储是独立服务”的部署一致。
- **客户端本地截止是保守的**：以授予响应中的 `expiry` 为准，暂停期间定时器
  被挂起，恢复后才触发 `client.lock_lost`；这模拟真实客户端“恢复后才发现
  自己可能已过期”，且它仍会尝试写——由资源兜底。
- **确定性**：模拟器使用自带 PCG RNG，相同 `seed` 跨机器/重复运行结果一致。
- 本项目是教学/验证性质的模拟器，不是生产锁实现；重点是把故障与不变量
  可观测、可复现地展示出来。
