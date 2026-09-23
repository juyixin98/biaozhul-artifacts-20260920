# 带围栏租约锁：确定性离散事件模拟器

纯后端项目。用 Go 实现一个**单进程、确定性、逻辑时钟驱动**的离散事件模拟器，
演示经典的 *fencing token（围栏令牌）* 租约锁：锁服务授予带**单调递增围栏号**的
租约，受保护资源**拒绝围栏号更小的写入**。当旧持有者因续约丢失或进程暂停而失去锁、
却仍继续运行（僵尸持有者）时，资源侧的围栏检查是最后一道防线。

**不依赖任何真实集群、真实网络、goroutine 并发或墙钟**：所有节点是引擎里的对象，
时间由模拟器推进，网络故障（丢包 / 重复 / 延迟乱序）和节点暂停都由场景 JSON 精确脚本化。

---

## 它证明了什么

> 给定租约 TTL、可丢包/重复/乱序的网络、可被冻结的节点，**资源永远不会接受一个围栏号
> 小于已见最大围栏号的提交**，即使该提交来自一个“自以为还持锁”的旧持有者。

三个验收场景（见下）分别构造：

1. **续约丢失** — 连续心跳被网络丢弃 → 租约到期 → 他人获锁 → 旧持有者的写被拒。
2. **暂停超过租期** — 旧持有者在暂停期间无法续约且毫无察觉，恢复后用旧围栏写 → 被拒。
3. **消息迟到 + 重复** — 续约迟到（租约已过期）、重复续约不二次延期、重复的陈旧提交两次被拒。

每次运行自动校验 6 条安全不变量，结果写进报告的 `invariants` 字段。

---

## 目录结构

```
.
├── go.mod
├── cmd/sim/main.go            # CLI：场景 JSON -> 报告 JSON
├── internal/sim/
│   ├── engine.go              # 离散事件内核（事件堆、逻辑时钟、节点暂停/恢复、定时器）
│   ├── rng.go                 # 确定性 PRNG（故障概率只依赖 seed）
│   ├── network.go             # 网络模型：丢包/重复/延迟乱序，脚本化规则 + 概率故障
│   ├── messages.go            # 统一报文格式 Envelope 与类型/结果常量
│   ├── lock.go                # 锁服务：租约、TTL、递增围栏号、请求去重
│   ├── resource.go            # 受保护资源：围栏号校验（核心防线）+ 提交幂等
│   ├── client.go              # 客户端：固定节奏心跳续约、暂停、僵尸提交
│   ├── scenario.go            # 场景 JSON 结构
│   ├── runner.go              # 校验 + 装配 + 运行 + 报告
│   ├── invariants.go          # 6 条安全不变量的自动校验
│   ├── trace.go               # 有序事件日志
│   └── sim_test.go            # 自动化测试
├── scenarios/                 # 输入：场景（即“请求样例”）
│   ├── 0-happy-path.json
│   ├── 1-renewal-lost.json
│   ├── 2-pause-longer-than-ttl.json
│   └── 3-late-and-duplicate-messages.json
├── reports/                   # 输出：上述场景在本机的真实运行报告
└── docs/REQUESTS.md           # 请求 / 报文 / 故障规则的字段级样例
```

---

## 快速开始

需要 Go 1.22+（开发环境为 go1.22.2 linux/amd64）。

```bash
go build -o bin/sim ./cmd/sim

# 跑三个验收场景（完整报告含逐条 trace）
bin/sim -f scenarios/1-renewal-lost.json
bin/sim -f scenarios/2-pause-longer-than-ttl.json -summary
bin/sim -f scenarios/3-late-and-duplicate-messages.json -o /tmp/3.json

# 也可以从标准输入
cat scenarios/0-happy-path.json | bin/sim -f - -summary
```

退出码：`0` 全部不变量通过；`1` 有不变量失败；`2` 场景 JSON 非法。

字段、故障规则、报文结构的完整说明见 **[`docs/REQUESTS.md`](docs/REQUESTS.md)**。

---

## 模型设计

### 离散事件引擎（`engine.go`）

- 一个最小堆事件队列，事件按 `(时间, 类型, 插入序号)` 排序，**完全确定**。
- 逻辑时间只前进；没有任何 `time.Now()`、`Sleep`、真实 socket 或并发（引擎单线程派发）。
- 三种事件：消息送达、命名定时器、脚本动作；另有内部“恢复边界”事件保证暂停恰在
  `until` 时刻结束。
- **节点暂停**：暂停期间该节点的定时器不入队、所有到达它的消息被挂起；恢复瞬间按原顺序
  集中投递。冻结节点也不能产生任何流量。暂停/恢复本身不受冻结影响（否则节点永远醒不来）。
- **定时器用版本号惰性取消**：同名定时器重排只会让旧版本事件被丢弃，避免陈旧触发。

### 网络（`network.go`）

- 基础延迟在 `[min,max]` 内由确定性 PRNG 抽取。
- 两类故障可叠加：
  - **概率故障** `drop_prob` / `duplicate_prob`（只依赖 `seed`）；
  - **脚本化规则** `rules[]`：按源/目的/请求类型/时间窗精确命中，支持
    `drop` / `duplicate` / `delay`（`reorder` 同义）。
- 重复报文携带 `duplicate_of`；规则里的 `delay_ms` 对**原件和副本整体生效**，
  使“迟到的续约及其重复副本”都在过期后才到达。
- 所有丢包/重复/延迟都计入 `network_stats` 并写 trace。

### 锁服务（`lock.go`）

- 单把锁。授予租约时分配**全局严格递增围栏号**（1,2,3,…）与 `expire_at = now + ttl`。
- 续约把 `expire_at` 延长一个 TTL；锁被占用时 `acquire` 返回 `busy`（客户端可重试）。
- **惰性过期**：每次到达消息/到期定时器时检查，过期即腾空，允许新持有者获取。
- **请求幂等去重**：按客户端唯一 `req_id` 缓存 acquire/renew/release 的结果——
  网络重复的续约返回同样的成功但**不会二次延长租期**，重复的 acquire 不会多消耗围栏号。

### 受保护资源（`resource.go`）——核心防线

资源维护“已见过的最大围栏号” `maxFence`。对每个写请求：

| 条件 | 结果 |
|---|---|
| `fence <= 0` | 拒绝 `fence_zero`（从未持锁） |
| `fence < maxFence` | 拒绝 `stale_fence`（旧持有者） |
| `fence >= maxFence` | 提交，并推进 `maxFence` |

注意规则是**拒绝更小的**围栏号：同一持有者用相同围栏号连续多次写是合法的（非递减）。
同一 `req_id` 的重复提交返回 `duplicate`，不会二次应用。

### 客户端（`client.go`）

- 获锁后按**固定节奏**（从授予时刻起算，不随 ack 到达漂移）发心跳续约。
- 收到 `renew.err` 或恢复时发现已越过 `expire_at`，本地进入 `lost_lease`，普通 `submit`
  会自我拒绝；`force_submit` 则模拟**僵尸**——用内存里保留的最后一个围栏号强行提交，
  交给资源去否决。这把“客户端自觉”与“资源强制保证”两条机制清晰分开。

---

## 验收场景与真实结果

以下数字来自本仓库 `reports/` 中由 `bin/sim` 在本机实际生成的报告（逻辑毫秒）。
复现：`go build -o bin/sim ./cmd/sim && for f in scenarios/*.json; do bin/sim -f $f -summary; done`。

### 场景 1：续约丢失 `1-renewal-lost.json`

窗口内丢弃 A 的连续续约。结果：`grants=2`，提交围栏 `[1,2]`，拒绝 1 次。

```
  12  lock.grant        A 拿到 fence=1，expire_at=2012
 510  resource.commit   A 以 fence=1 提交 "a-write-1"
2733  lock.expire       A 的租约因续约全丢而到期
2846  lock.grant        B 拿到 fence=2
3015  resource.commit   B 以 fence=2 提交 "b-write-fence-2"
3461  resource.reject   A 用旧 fence=1 提交 → stale_fence（资源已见 2）
```

### 场景 2：暂停超过租期 `2-pause-longer-than-ttl.json`

TTL=1000，A 在 t=500 被冻结 2500ms。结果：`grants=3`，提交围栏 `[1,2,3]`，拒绝 1 次。

```
 500  node.pause        A 冻结到 3000（期间零心跳、零报文）
1438  lock.expire       A 的租约在冻结中到期
1477  lock.grant        B 拿到 fence=2
2212  resource.commit   B 以 fence=2 提交
3000  node.resume       A 恢复，挂起报文集中投递，本地判定 lost_lease
3108  resource.reject   A（僵尸）仍用 fence=1 提交 → stale_fence
3513  lock.grant        A 重新获锁，拿到严格更大的 fence=3
4110  resource.commit   A 以 fence=3 提交
```

### 场景 3：消息迟到与重复 `3-late-and-duplicate-messages.json`

丢两次续约 + 续约迟到 700ms 并被网络复制 + 陈旧提交被复制。结果：
`grants=3`，提交围栏 `[1,2,3]`，拒绝 2 次（同一陈旧提交的原件与副本），`dropped=2 duplicated=2`。

```
2010  lock.expire        A 租约到期（续约丢在网络里）
2030  lock.grant         B 拿到 fence=2
2610  resource.commit    B 以 fence=2 提交
2830  lock.renew.expired A 迟到的续约到达 ×2 → 都被告知 expired（重复不复活租约）
3460  resource.reject    A 的陈旧提交 fence=1 → stale_fence（原件）
3510  resource.reject    同一提交的重复副本 fence=1 → stale_fence（副本）
4810  resource.commit    A 重新获锁后以 fence=3 提交
```

另有 `0-happy-path.json`：正常获取/续约（含一次被去重的重复续约）/释放/再获取，
提交围栏 `[1,2]`，无拒绝。

### 自动校验的不变量（每个场景 + 模糊测试都必须通过）

1. `fence_tokens_strictly_increasing`：锁发出的围栏号严格 1,2,3,…
2. `committed_fences_never_decrease`：资源提交序列围栏号绝不下降（相等=同持有者多次写，合法）
3. `rejections_only_for_stale_or_fenceless`：被拒的一定是陈旧或无围栏写（不误伤新持有者）
4. `every_commit_has_fence`：任何提交都带正围栏号
5. `lease_not_overgranted_while_alive`：前一租约未过期/未释放时不会发放新租约
6. `no_stale_fence_ever_committed`：trace 中任何提交围栏号都不低于当时资源 epoch

---

## 自动化测试

```bash
go test ./...            # 10 个测试全部通过
go test -race ./...      # 竞态检测通过（引擎为确定性单线程派发）
go vet ./...
gofmt -l .               # 无输出
```

测试覆盖（`internal/sim/sim_test.go`）：

- 三个验收场景的**断言级**版本（授予数、提交围栏序列、拒绝原因/次数、暂停期间零流量）。
- `TestDuplicateRenewDoesNotExtendTwice`：重复续约不二次延长租期。
- `TestDeterminism`：同输入连跑三次，报告**逐字节相同**。
- `TestInvariantsUnderProbabilisticFaults`：40 个不同种子 + 12% 丢包 + 10% 重复的随机故障
  模糊测试，6 条不变量在每个种子下都成立。
- `TestRejectFenceZero`、`TestValidation`（8 类非法场景）、`TestClockDoesNotUseWallTime`
  （6000 逻辑毫秒在远小于真实 6000ms 内跑完）、`TestScenarioFilesOnDisk`（所有随附场景）。

### 实际运行记录（如实）

开发与验证环境：**go1.22.2 / linux/amd64**。

| 命令 | 结果 |
|---|---|
| `go build ./...` | 通过 |
| `go vet ./...` | 无告警 |
| `gofmt -l .` | 无输出（已格式化） |
| `go test -race -count=1 ./...` | `ok fencingleasesim/internal/sim`，10/10 PASS |
| 四个场景 `bin/sim` | 退出码均为 0，`all_invariants_pass=true` |
| 同场景连跑两次 `cmp` | 报告逐字节一致（确定性） |
| 非法 JSON（`{"name":"bad"}`） | 退出码 2，报 `scenario.ttl_ms must be positive` |

**未通过项 / 局限（如实说明）：**

- 本项目为**模拟器**，不提供网络服务端口、不做前端，也不模拟真实磁盘 I/O；
  “资源”是进程内对象。这是任务刻意要求的边界。
- 锁服务建模为**单节点**（无共识复制、无领导者选举）；围栏方案本身解决的是
  “旧持有者在锁易主后继续写”的问题，而非锁服务自身的高可用。多副本锁服务不在范围内。
- `duplicate` 规则只额外生成一份副本（可调网络参数扩展为多份）；概率故障在极个别种子下
  可能让某个脚本化动作恰好无影响——这也是模糊测试要遍历多种子的原因。
- 开发过程中曾发现并修正两处真实缺陷（已修复并有回归测试）：①重复故障的原件未继承
  延迟，导致“迟到续约”的副本提前救回租约；②初版把资源提交误判为“围栏必须严格递增”，
  实际上同持有者相同围栏号可多次写，正确规则是**拒绝更小的**围栏号（非递减）。
