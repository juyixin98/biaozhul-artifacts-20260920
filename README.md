# chsim — 一致性哈希迁移的确定性离散事件模拟器

纯后端项目（Go，无任何外部依赖，无真实集群、无前端）：在**单进程**内用确定性离散
事件模拟一个带权重和虚拟节点的一致性哈希 KV 存储，演示并**自动验收**集群扩容、
并发写入、迁移中断、节点移除等过程中数据的归属与版本正确性。

- 环：虚拟节点 + 权重，环变更按哈希区间计算最小迁移任务集合。
- 迁移期间：**双写**（旧主 + 新主都确认才算写入确认）、**双读**（读两边，单一版本
  决胜：版本号最大者胜）。
- **切换屏障（barrier）**：所有区间迁移全部 ACK 后，epoch +1，旧环才被丢弃；屏障前
  发出的请求排空后，离开中的节点才允许下线。
- 安全移除：节点下线前逐键校验“当前环属主持有版本 ≥ 该节点版本”，保证
  **已确认数据不会只存在于被移除节点**。
- 网络可独立注入**丢包、重复、乱序**；所有消息超时按当前阶段幂等重传。
- 同一场景重复运行产出**字节级一致**的结果（单种子 RNG、单一事件循环）。

## 目录结构

```
cmd/chsim/main.go        命令行入口：单次场景运行 / HTTP 服务
internal/ring/           加权虚拟节点一致性哈希环 + 迁移计划（纯函数）
internal/sim/            事件循环、网络、节点、协调器、客户端、验收校验
internal/api/            JSON/HTTP 接口（POST /run, GET /healthz）
examples/                验收场景样例（扩容/移除/中断/混沌网络）
requests/                HTTP 请求样例
```

## 核心设计

### 1. 确定性离散事件模拟

- 逻辑时钟单位为毫秒；事件按 `(时间, 序列号)` 排序，同刻事件按入队顺序执行。
- 唯一的随机源是由 `seed` 初始化的一个 `math/rand`；无墙钟、无 goroutine、
  无锁。同一场景任意重跑结果完全一致（测试 `TestDeterministic` 逐字节比较）。
- 所有节点、协调器都只是进程内对象，通过模拟网络收发消息。

### 2. 加权虚拟节点环

- 节点 `weight=w` 拥有 `vnodes*w` 个虚拟节点；键的属主是哈希顺时针方向第一个 vnode。
- vnode 与键使用同一哈希：`fmix64(FNV-1a64(x))`。FNV-1a 对短且后缀相似的字符串
  （如 `n1#0..n1#127`、`key-0000..`）雪崩性极差、会整体聚簇到一个小区间，因此
  叠加了 MurmurHash3 的 `fmix64` 双射终化（只用标准库）。这是开发中实测发现的
  问题，见下文“开发中发现并修复的问题”。
- 迁移计划：合并新旧两环的全部 vnode 边界，对每个最大区间 `(p_i, p_{i+1}]` 比较
  新旧属主，仅在属主变化时生成一条 `src→dst` 的区间任务（含跨零回绕区间）。
  加节点时所有任务的 `dst` 都是新节点，删节点时所有任务的 `src` 都是被删节点。

### 3. 迁移协议（关键不变量）

- **版本**：协调器对每次写入加盖一个全局单调版本号；节点按 `PutIfNewer` 落盘
  （版本大者胜，同版本是同一次写入的幂等重放）。
- **双写**：迁移态下，若键的新旧属主不同，写请求同时发给两者，**两者都 ACK 才向
  客户端确认**。因此任何“已确认”的写入在屏障前始终同时存在于两侧。
- **双读 + 单一版本决胜**：迁移态同时读新旧属主，取返回记录中版本号最大的一份。
- **区间迁移**：协调器按并发度（`transfer.concurrency`）分页拉取源节点区间内的键
  （fetch → batch → transferPut → ack），逐页推进游标；`PutIfNewer` 保证重复传输
  幂等且不会回退版本。
- **切换屏障**：全部任务完成是唯一的切换点。屏障发生时：迁移态结束、`epoch++`、
  丢弃旧环，之后新请求只走新环；屏障前已在途的请求继续排空。
- **安全移除**：被移除节点在屏障前仍以 leaving 身份正常服务；屏障后且所有旧 epoch
  在途请求计数归零，协调器逐键校验“新环属主的版本 ≥ 该节点版本”，通过后才发
  Decommission。下线过程本身幂等——已关机节点对重复的 Decommission 仍回 ACK，避免
  单次 ACK 丢失导致永久等待。
- 迁移中再次收到的拓扑变更按 FIFO 排队，在下一个屏障后开始。

### 4. 网络故障模型

每条消息独立判定：

- `drop_rate`：静默丢弃（发送方靠超时重传）；
- `dup_rate`：额外再投递一份；
- 每份拷贝独立附加 `base_delay + uniform[0,jitter]` 的随机时延，自然形成乱序。

读/写/迁移/下线消息均有超时与重传代次（generation），过期代次的回复被忽略。

## JSON 运行接口

### 请求：`POST /run`

请求体为场景 JSON（字段定义见 `internal/sim/types.go`）：

| 字段 | 说明 |
|---|---|
| `seed` | 随机种子，决定所有丢包/重复/时延 |
| `vnodes` | 每单位权重的虚拟节点数（默认 64） |
| `nodes` | 初始成员 `[{id, weight}]`，`weight<=0` 按 1 处理 |
| `network` | `base_delay_ms` / `jitter_ms` / `drop_rate`∈[0,1) / `dup_rate`∈[0,1) |
| `transfer` | `batch_size` / `concurrency` / `fetch_timeout_ms` |
| `op_timeout_ms`, `max_attempts` | 客户端请求超时与最大重传次数（默认 200/30） |
| `clients` | `id,start_ms,ops,interval_ms,keys,read_every`；操作 j 访问 `key-(j mod keys)`，每 `read_every` 次做一次读 |
| `ops` | 定时控制操作：`add_node` / `remove_node` / `pause_migration` / `resume_migration` |

`pause_migration`/`resume_migration` 用于模拟迁移中断（协调器暂停派发新区间；
在途消息继续完成）。

### 响应：结果 JSON

- `stats`：确认/失败读写数、消息收发/丢弃/重复/重传统计、迁移键数与字节数、
  逐次屏障（epoch、时间、任务数、迁移量）、下线节点数。
- `verification`：
  - `key_issues`：每个已确认键的**最终属主**是否恰好持有最高确认版本与值；
  - `stale_reads`：读是否返回了早于“读发起时已确认版本”的旧数据；
  - `removal_violations`：下线时是否存在“最新版本仅在被移除节点”的键；
  - `failed_ops`：重传预算耗尽的请求数；
  - `pass`：以上全部为空且无场景错误才为 `true`。

非法 JSON / 非法参数返回 `400 {"error": ...}`。

## 构建与运行

需要 Go 1.22+（本仓库在 `go1.22.2 linux/amd64` 实测）。

```bash
go build ./...
go vet ./...

# 方式一：单次运行场景，输出结果 JSON（验收失败时进程返回非零）
go run ./cmd/chsim -scenario examples/scale_out.json

# 方式二：HTTP 服务
go run ./cmd/chsim -listen 127.0.0.1:8080
#   POST /run      请求体为场景 JSON
#   GET  /healthz  -> ok
```

## 验收场景与实测结果

以下均为本机真实执行记录（逻辑时间，非墙钟；seed 已固定，可复现）。

```text
$ go test -count=1 -race ./...
?       chsim/cmd/chsim  [no test files]
ok      chsim/internal/api       (race)
ok      chsim/internal/ring      (race)
ok      chsim/internal/sim       (race)
```

| 场景 | 文件 | 内容 | 结果 |
|---|---|---|---|
| 扩容 | `examples/scale_out.json` | 3 节点（权重 1/1/2）+ 3 个并发客户端持续读写，t=600 加入 n4 | pass，720/720 写确认，0 陈旧读，迁移 15 键/210B，128 个区间任务，t=1007 屏障 |
| 节点移除 | `examples/remove_node.json` | 4 节点持续写入后 t=1200 移除 n1 | pass，400/400 写确认，迁移 7 键，n1 安全下线，0 移除违规 |
| 迁移中断 | `examples/interrupt.json` | t=400 扩容，t=410 暂停、t=900 恢复，期间持续读写 | pass，675/675 写确认，屏障发生在恢复之后（t=1348），0 陈旧读 |
| 混沌网络 | `examples/chaos.json` | 丢包 25%、重复 12%、大抖动；扩容中断后再移除节点 | pass，600/600 写确认，2 次屏障，1 节点下线，1202 丢弃 / 453 重复 / 984 重传 |

扩容场景实测输出（节选）：

```json
{
  "stats": {
    "writes_issued": 720, "writes_confirmed": 720, "writes_failed": 0,
    "reads_issued": 180, "reads_completed": 180, "stale_reads": 0,
    "messages_dropped": 60, "messages_duplicated": 51, "retries": 59,
    "keys_migrated": 15, "unique_keys_migrated": 15, "bytes_migrated": 210,
    "barriers": [{ "epoch": 2, "time_ms": 1007, "tasks": 128,
                  "keys_migrated": 15, "bytes_migrated": 210 }]
  },
  "verification": { "pass": true, "keys_checked": 64,
    "key_issues": [], "stale_reads": [], "removal_violations": [], "failed_ops": 0 },
  "final_nodes": ["n1", "n2", "n3", "n4"], "final_epoch": 2
}
```

HTTP 接口实测：

```text
$ curl -s http://127.0.0.1:8080/healthz
ok
$ curl -s -X POST --data-binary @examples/scale_out.json http://127.0.0.1:8080/run \
    | python3 -c 'import json,sys;r=json.load(sys.stdin);print(r["verification"]["pass"])'
True
$ curl -s -o /dev/null -w '%{http_code}\n' -X POST -d '{bad' http://127.0.0.1:8080/run
400
```

确定性实测：同一场景连续两次 CLI 运行，输出文件 `cmp` 字节一致
（`TestDeterminism` 在测试中做同样的比较）。

## 自动化测试

```bash
go test -count=1 -v ./...
```

- `internal/ring`（7 个）：属主稳定性、权重倾斜、扩容只发往新节点、缩容只从被删
  节点迁出、计划对每个变更键的区间覆盖、回绕区间边界。
- `internal/sim`（10 个）：扩容、安全移除、迁移中断、小键空间强争用并发写、
  30% 丢包混沌网络、**多页迁移（大键空间+小批次）**、扩容+中断+移除串联、
  确定性复现、最后节点禁删、参数校验。
- `internal/api`（3 个）：`POST /run` 正常验收、非法 JSON 返回 400、`/healthz`。

## 开发中发现并修复的问题（如实记录）

这些问题都被自带测试在开发过程中抓出并修复，当前测试全部通过；记录于此以便审计：

1. **哈希聚簇导致环失效**。初版直接用 FNV-1a64 定位 vnode 与键，实测 200 个
   `key-XXXX` 全部落在同一个节点上（FNV 对短/后缀相似字符串雪崩性差）。修复：
   叠加 `fmix64` 终化，vnode 与键统一处理；加了属主分布测试。
2. **迁移批次超时会跳过未确认数据**。初版在 fetch/put 任一轮超时后释放槽位并从
   推进后的游标重拉，若 transferPut 的 ACK 丢失，整批键永久缺失（测试中表现为
   最终属主无副本 + 移除违规）。修复：超时不推进游标、按当前阶段幂等重发同一批
   消息。
3. **下线 ACK 丢失导致协调器永久重传**。节点收到首个 Decommission 即关机，重传的
   Decommission 被死节点静默丢弃、永不回 ACK，协调器重试到事件预算耗尽（测试中
   出现约百万次重试）。修复：Decommission 对已关机节点也重复回 ACK（幂等关机语义）。
4. **多页迁移的并发槽计数泄漏（自查发现，初版测试未覆盖）**。最初每页 fetch 都
   `inflight++`、只有任务结束才减，一旦某个区间的数据超过一页，槽位被迅速占满、
   迁移永久停滞（且所有写仍确认，不查迁移量就发现不了）。修复：并发槽在任务激活
   时占用、任务完成时释放，翻页只发下一条 fetch；新增 `TestMultiPageMigration`
   （2000 键、批次 3）并已验证它在旧实现上确实失败（0 屏障、迁移卡死）。顺带用
   单调 `timerSeq` 作废跨阶段残留的超时定时器，避免 fetch 定时器在 put 阶段形成
   第二条重传链。

## 已知限制与取舍

- 模拟的是单协调器、单副本（RF=1）协议；协调器“崩溃”建模为 `pause/resume`（暂停
  派发、在途消息继续、恢复后续传），没有实现独立的持久化日志。
- 迁移完成后旧属主上的冗余副本不做主动清理（缩容节点除外，其在排空后下线）。
- 读写路由经过协调器；没有模拟客户端持有陈旧环视图直连节点的情形。
- 无任何持久化、鉴权与前端，仅用于协议正确性的教学与验收。
