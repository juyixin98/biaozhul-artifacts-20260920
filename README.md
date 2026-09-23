# chmig — 带虚拟节点与权重的一致性哈希在线迁移模拟器

纯后端、单进程、**确定性离散事件模拟器**，用于验证一致性哈希环在**扩容、并发写入、
节点移除（缩容）和迁移中断**下的正确性。没有真实网络、没有 goroutine、没有真实
集群：所有"节点"都是同一进程内的 actor，所有消息都经过一个可注入**丢包 / 重复 /
乱序 / 黑洞**的模拟网络；相同配置 + 相同随机种子必然产生相同结果。

## 它验证什么

1. **带权重和虚拟节点的一致性哈希环**（`ring` 包）：FNV-1a + splitmix64 雪崩化，
   权重按比例分配虚拟节点；新增一个节点只迁移约 `1/(N+1)` 的键。
2. **在线迁移协议**（`sim` 包）：旧属主 A 对每个迁出键走
   `BEGIN → SNAPSHOT → FORWARD(并发写) → 切换屏障 BARRIER → COMMIT`。
3. **迁移期双读、单一版本决胜**：客户端同时读旧/新属主，取**版本号最大**的应答；
   并带每客户端单调读水位线，绝不接受比已见过版本更旧的结果。
4. **切换屏障（switch barrier）**：A 在屏障点冻结该键，B 必须连续应用到冻结点之前的
   全部前向写并回 ACK；屏障通过但未 COMMIT 前，A/B **都不确认**新写——这保证中断时
   已确认数据仍只在权威属主上。
5. **节点移除安全闸门**：缩容提交前，控制器逐键证明被移除节点已不持有任何键的最新
   记录、存活属主已持有该最新版本；闸门不通过则拒绝提交、拒绝下线该节点。
6. 端到端五项不变量（见报告 `verifications`）。

## 目录

```
ring/                 一致性哈希环（不可变拓扑视图、Diff）
sim/                  事件引擎、模拟网络、节点/客户端协议、账本与验证、JSON 类型
cmd/chmig/            JSON 配置 → JSON 报告 的命令行入口
examples/             三个验收场景的请求样例
```

## 快速开始

需要 Go 1.22+，无第三方依赖。

```bash
go test ./...                 # 全部自动化测试（环性质 + 模拟场景 + 多种子压测）

# 三个验收场景（-strict：任一校验失败则退出码非 0）
go run ./cmd/chmig -in examples/scaleout.json     -strict
go run ./cmd/chmig -in examples/scalein.json      -strict
go run ./cmd/chmig -in examples/interrupted.json
```

也可从标准输入读配置：

```bash
cat examples/scaleout.json | go run ./cmd/chmig -in -
go run ./cmd/chmig -in examples/scaleout.json -out report.json
```

## 迁移协议（单进程模拟）

对每个属主发生变化的键，由控制器（`ctrl`）编排：

```
ctrl ──BEGIN(key)──────────▶ A(旧属主)
A    ──SNAPSHOT(ver,value,幂等表)──▶ B(新属主)
A    ──FORWARD(seq, ver, value)───▶ B   # 迁移窗口内到达 A 的每个写，应用后立即前传
ctrl ──BARRIER(key, upTo)──▶ A          # A 冻结该键，不再本地应用/确认
A    ──MIG_BARRIER(upTo)────▶ B
B    连续应用到 seq=upTo 后 ──MIG_BARRIER_ACK──▶ A
A    ──BARRIER_DONE─────────▶ ctrl
ctrl 收齐全部键 ── COMMIT(新环) ──▶ 所有节点/客户端（可靠重传直到全部 ACK）
     COMMIT 后 B 才落盘并确认屏障期间缓冲的写；A 删除迁移态
缩容时：COMMIT 后 ctrl 再发 DECOMMISSION，存活属主通过移除闸门才允许节点下线
```

关键安全性质：

- **幂等**：每条客户端写有全局唯一 `ReqID`；节点按 `ReqID` 去重，重传/重复投递/
  迁移后重试都不会重复应用。幂等应答按**该版本**取值，绝不串用更新版本的值。
- **单调版本**：每个键在属主上的版本号严格 +1；快照/前传携带版本链，B 按序号连续
  应用，乱序到达先缓冲。
- **屏障即边界**：冻结后到 COMMIT 之间，A 只重定向、B 只缓冲，**双方都不确认**新写。
  因此迁移在 COMMIT 前被打断时，未决写全部"未确认"，而已确认写只在旧权威属主上——
  不会出现"已确认数据仅存在于即将消失的节点"。
- **陈旧消息隔离**：已提交 epoch 的迟到 SNAPSHOT/FORWARD/BARRIER 会被节点直接忽略，
  不会用旧值回退已提交存储。

## JSON 运行接口

### 请求（Config）

见 `examples/*.json`。主要字段：

| 字段 | 含义 |
|---|---|
| `seed` | 随机种子；决定网络丢包/延迟与客户端选键，保证可复现 |
| `vnodesPerWeight` | 权重 1 对应的虚拟节点数 |
| `preload` | tick 0 给每个键写一条种子记录，使迁移有真实快照数据、迁移量可统计 |
| `lossRate` / `duplicateRate` / `reorderRate` | 每包独立的丢包 / 重复 / 乱序概率，范围 `[0,1)` |
| `minLinkDelay` / `maxLinkDelay` | 单向投递随机延迟（tick） |
| `retryTicks` | 客户端请求与迁移控制消息的重传间隔 |
| `migrationConcurrency` | 单个旧属主并发迁移键的调度上限（错峰开始） |
| `barrierTicks` | BEGIN 到冻结屏障的窗口长度 |
| `drainTicks` / `stopTick` | 收尾时长；`stopTick=0` 时用 `最后操作tick+drainTicks` |
| `initialNodes[]` | 初始加权节点 `{id, weight}` |
| `keys[]` | 键的全集（最终归属按全集校验） |
| `clients[]` | 客户端写流 `{id, writes, startTick, endTick, readEvery}` |
| `operations[]` | 定时操作，见下 |

`operations[].kind`：

- `scaleOut`：`{tick, add:[{id,weight}]}`，新节点加入并触发迁移。
- `scaleIn`：`{tick, remove:[id]}`，节点离开；受移除安全闸门保护。
- `interrupt`：`{tick, endTick}`，在该时间窗内**黑洞所有节点间迁移流量**
  （`Mig*` 消息），但客户端/控制流量照常——用于观察重传恢复与"未提交不丢数据"。

### 响应（Report）

- `finalRing[]`：最终权威环及每个在线节点实际持有的键。
- `removedNodes[]`：已安全下线的节点。
- `topologyEpochs[]`：每个环版本的开始/提交时刻；未提交的 epoch `commitAt=0`。
- `stats`：报文收发/丢弃/重复/乱序、字节数、读写 issued/confirmed/failed。
- `migration`：迁移量统计，见下。
- `verifications`：五项端到端校验，全部 `pass:true` 才算通过。

迁移量字段：

| 字段 | 含义 |
|---|---|
| `keysMoved` | 在**已提交** epoch 中改主的键数（中断未提交不计入） |
| `keysSwitched` | 屏障已通过的键数（可能含未提交 epoch），`keysMoved ≤ keysSwitched` |
| `epochsCommitted` | 成功提交的拓扑版本数 |
| `recordVersionsTransferred` | 经快照/前传在新属主落盘的记录版本总数 |
| `bytesTransferred` | 上述版本的净荷字节总数 |
| `bytesMoved` | 每个迁出键最终记录净荷各计一次：不可避免的最小迁移量 |
| `overheadBytes` | `bytesTransferred - bytesMoved`：迁移窗口内并发写带来的额外前传 |
| `movements[]` | 每个键的 `from/to/ringVersion/beganAt/switchedAt/committed` |

五项校验：

1. `ownership`：每个曾被写入的键物理存在于最终环的属主上。
2. `version`：属主持有的是全局账本记录的最新版本与正确值（单一版本决胜）。
3. `confirmedWritesSurvive`：每条收到 ACK 的写在最终属主上以其版本（或更新链）存在。
4. `removalSafety`：被移除节点不持有任何键的唯一最新副本（闸门 + 事后复核）。
5. `noStaleReadAccepted`：单调读（同客户端不见版本倒退）且返回的 `(版本,值)` 真实存在。

## 确定性

- 单一逻辑时钟 + 按 `(时刻, 入队序号)` 排序的最小堆事件队列，单线程执行。
- 工作负载由"主种子派生的每客户端 RNG"在装配阶段一次性生成，网络调度不会扰动负载。
- `TestDeterminism` 断言同一配置两次运行的 JSON 报告逐字节一致。

## 已知边界（模拟范围之外）

- 副本因子固定为 1（RF-1）；不建模持久化磁盘、真实 TCP、跨进程故障恢复。
- 不实现读仲裁多数派；读一致性通过双读取最高版本 + 客户端单调水位线实现。
- `interrupt` 只黑洞节点间迁移流量（刻意保留控制/客户端面，以观察协议自愈）。
