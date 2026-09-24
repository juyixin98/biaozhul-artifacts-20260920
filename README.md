# merklekv — 反熵 Merkle 同步（Go / net/http）

两个键值副本之间的**反熵（anti-entropy）差异定位与同步**服务。固定分区
Merkle 树用于自顶向下定位变化的桶，只交换变化路径上的哈希和变化桶内的
记录；支持删除墓碑、版本比较（HLC + LWW），并在扫描期间树根发生变化时
检测冲突、丢弃本轮结果、用新的一致快照重试。

纯后端、无界面、**零第三方依赖**（仅 Go 标准库：`net/http`、`crypto/sha256`
等）。

---

## 1. 它解决什么问题

朴素同步需要把一个副本的全部数据发给另一个副本（O(全部数据)）。反熵
Merkle 同步把"找出差异"和"传输数据"分开：

```
副本A                                副本B
  │  1. 各自取不可变快照 + 算树根      │
  │ ───────── 比较 32 字节根哈希 ────► │
  │  根相同 → 零数据，结束             │
  │  根不同：                           │
  │  2. 自顶向下逐层要子节点哈希       │
  │     哈希相同的子树整棵剪枝，跳过    │
  │  3. 只对最终不同的叶子(桶)         │
  │     拉取桶内完整记录（O(差异)）     │
  │  4. 桶内按版本做 LWW 并集合并       │
  │  5. 用 epoch 守卫双向提交           │
  │     任一侧树根在期间变了 → 整轮重试 │
```

传输量是 **O(变化桶数 × 桶大小 + 树路径哈希数)**，与总数据量无关
（见第 7 节实测）。

---

## 2. 目录结构

```
go.mod                          模块声明（无 require 块 = 依赖闭包已锁定）
cmd/merklekv/main.go            HTTP 服务入口（一个进程 = 一个副本）
internal/hlc/                   混合逻辑时钟（版本号）
internal/store/                 版本化 KV、墓碑、epoch、不可变快照
internal/merkle/                固定分区 Merkle 树（构建/导航/预测根）
internal/server/               net/http 路由、JSON 协议、树缓存
internal/sync/                  反熵同步客户端（BFS 差异定位 + 重试）
Makefile
```

---

## 3. 依赖与构建

- **Go**：1.23（`go.mod` 声明 `go 1.23`；仅用标准库，1.21+ 的 `http.ServeMux`
  方法路由是最低要求，建议直接用 1.23）。
- **第三方依赖：无。** `go mod tidy` 后不产生 `go.sum`；
  `go mod vendor` 输出 `no dependencies to vendor`。依赖闭包天然锁定、
  可完全离线构建。

```bash
make build      # 生成 bin/merklekv
# 或
go build -o bin/merklekv ./cmd/merklekv
```

---

## 4. 启动

两个副本就是两个进程（不同监听地址、不同副本 ID）：

```bash
bin/merklekv -listen 127.0.0.1:8080 -replica-id r1 -fanout 16 -depth 3
bin/merklekv -listen 127.0.0.1:8081 -replica-id r2 -fanout 16 -depth 3
```

| 参数 | 默认值 | 说明 |
|---|---|---|
| `-listen` | `127.0.0.1:8080` | 监听地址 |
| `-replica-id` | `r1` | 副本稳定 ID，用于 LWW 同时间戳的确定性决胜 |
| `-fanout` | `16` | 树的叉数（2..256） |
| `-depth` | `3` | 树深度（1..10），桶数 = fanout^depth（默认 4096） |
| `-snapshot-ttl` | `30s` | 已固定快照的保留时间 |

副本数不限制；任意副本都可以向任意其他副本发起同步。

---

## 5. HTTP 接口

所有请求/响应为 JSON（KV 值除外，KV 读写用原始字节 body）。

### 5.1 键值操作

```bash
# 写入（原始字节为值；服务端分配单调递增的 HLC 版本）
curl -s -X PUT --data-binary 'hello world' localhost:8080/v1/kv/user:1
# 读取
curl -i localhost:8080/v1/kv/user:1
# 删除（写墓碑；对不存在的键返回 201，表示记录了墓碑）
curl -s -X DELETE localhost:8080/v1/kv/user:1
```

### 5.2 同步协议（手工驱动的四步）

**第 1 步：两侧各取快照**（响应里的根哈希随快照一起返回，不额外传输）

```bash
curl -s -X POST localhost:8080/v1/snapshots
# {"snapshot_id":"ab12…","epoch":7,"root_hash":"…","records":100,"bytes":…,"wire_bytes":…}
curl -s -X POST localhost:8081/v1/snapshots
```

根哈希相同 → 两侧内容完全一致，无需任何后续操作。

**第 2 步：批量请求树节点哈希并自顶向下比较**

```bash
curl -s -X POST localhost:8081/v1/nodes -H 'Content-Type: application/json' -d '{
  "snapshot_id": "<r2-snapshot-id>",
  "paths": [""]
}'
# 根路径为 ""；其 children 是 fanout 个子哈希。
# 只对与本地不同的子哈希继续请求，例如 "7"、"7/3"、"7/3/12"（叶子）。
curl -s -X POST localhost:8081/v1/nodes -H 'Content-Type: application/json' -d '{
  "snapshot_id": "<r2-snapshot-id>",
  "paths": ["7/3/12"]
}'
# 叶子节点带 "leaf":true, "bucket":1964, "count":5
```

**第 3 步：只拉取不同叶子桶的完整记录**

```bash
curl -s -X POST localhost:8081/v1/entries -H 'Content-Type: application/json' -d '{
  "snapshot_id": "<r2-snapshot-id>",
  "buckets": [1964]
}'
```

**第 4 步：带 epoch 守卫提交合并结果**

```bash
curl -s -X POST localhost:8081/v1/apply -H 'Content-Type: application/json' -d '{
  "expected_epoch": 7,
  "entries": [
    {"key":"user:1","value":"...","ver":{"wall":1727000000000,"log":3},"origin":"r1"}
  ]
}'
# 200 {"applied":1,"unchanged":0,"epoch":8,"root_hash":"…","changed":true}
# 409 {"error":"epoch_moved","epoch":9}   ← 树根在扫描期间变了，整轮重来
# 404 {"error":"snapshot_not_found", …}   ← 快照过期/被驱逐，整轮重来
```

### 5.3 一键触发同步（演示用）

把第 5.2 步整套流程内置在了客户端里：

```bash
curl -s -X POST localhost:8080/v1/sync -H 'Content-Type: application/json' \
  -d '{"peer":"http://127.0.0.1:8081","max_rounds":10}'
```

响应统计了本次实际交换量：

```json
{
  "converged": true,
  "rounds_attempted": 1,
  "root_moves_detected": 0,
  "hash_nodes_exchanged": 4,
  "buckets_opened": 1,
  "entries_exchanged": 1,
  "user_bytes_exchanged": 310,
  "wire_bytes_exchanged": 4498,
  "baseline_full_transfer_bytes": 26912,
  "duration": 3000000
}
```

### 5.4 调试 / 测试辅助

```bash
curl -s localhost:8080/v1/config                 # 参数与当前 epoch
curl -s localhost:8080/v1/debug/state            # 全部记录（含墓碑）
curl -s 'localhost:8080/v1/debug/tree'           # 整棵树所有节点哈希
curl -s -X POST localhost:8080/v1/admin/seed -H 'Content-Type: application/json' \
  -d '{"entries":[{"key":"k","value":"...","ver":{"wall":1,"log":0},"origin":"r1"}]}'
```

`/v1/admin/seed` 直接安装带指定版本的初始数据、不 bump epoch，用于造测试
场景；生产环境可移除该路由。

---

## 6. 关键设计

### 6.1 固定分区

键用 FNV-1a 映射到 `N = fanout^depth` 个固定桶：`bucket = FNV1a(key) mod N`。
桶是一棵完整 fanout 叉树的叶子。分区方案两侧完全一致且与数据无关，因此：

- 两副本对同一个键永远落到同一个桶；
- 空子树哈希相同，稀疏数据也很便宜；
- 桶不随增删键而分裂/合并（对比动态分片），协议无状态。

叶子哈希覆盖桶内每条记录的 **key、value、墓碑标志、HLC 版本、origin**，
所以改值、改版本、删除、复活都会改变叶哈希。内部节点哈希带层号做域分离，
防止不同层之间的哈希碰撞。

### 6.2 版本比较与墓碑

- 版本号是 **HLC `(wall, logical)`**，副本内严格单调；收到更高的外部版本
  会把本地时钟前推（`Clock.Observe`），保证后续本地写排在复制写之后。
- 合并是**逐键 LWW**：版本高者胜；版本完全相同用 origin 字符串次序决胜，
  两侧规则一致、对称、收敛。
- 删除写**墓碑**（`deleted=true` 的记录），墓碑参与哈希与复制，因此
  "删除"和"更新"对协议是同一种变化；更新的存活版本可以覆盖旧墓碑（复活）。

### 6.3 一致快照与"扫描期间更新"

- 每次变更 bump 一个 `epoch` 计数；快照是不可变拷贝，按 TTL/容量保留。
- 一轮同步在**开始时**两侧各钉住一个快照，整轮比较都基于这两个快照。
- 提交时双向带 `expected_epoch` 守卫：
  - 对端在扫描期间被写入 → 对端 apply 返回 **409 epoch_moved**；
  - 本地在扫描期间被写入 → 本地 apply 返回 epoch 不匹配；
  - 快照 TTL 过期/被驱逐 → 请求返回 **404 snapshot_not_found**。
  任一情况都**丢弃本轮**（守卫保证没有任何一侧部分写入），指数退避后用
  全新快照整轮重试，最多 `max_rounds`（默认 10）。
- 提交前客户端还会用 `Tree.ExpectedRoot` **预测**合并后的树根，对端 apply
  响应回传其新根，不一致（例如存在第三方写入）同样触发重试。
- LWW 并集是交换律、幂等的，所以重试不会丢数据或让墓碑复活。

---

## 7. 实测结果

命令：`go test -race -count=1 ./...`（环境：Go 1.23.4，linux/amd64）。
全部测试通过，`-race` 无数据竞争。

### 7.1 三个验收场景（`internal/sync` 集成测试，真实 HTTP）

| 场景 | 结果 |
|---|---|
| 单叶变化（2000 键、每值 1 KiB，仅改 1 键） | 交换 **4 个哈希节点、开 1 个桶、2 条记录**，线网 **5923 B = 全量基线 5.77 MB 的 0.103%**，0 次重试，最终收敛 |
| 全量差异（两侧各 600 个互不相交的键，256 桶） | 开 256 个桶、交换 1200 条记录后收敛为 1200 键并集；**紧接着再同步：0 桶、0 记录**（根哈希即足够） |
| 扫描期间更新（发起方/对端分别在 BFS 与 apply 之间被写入） | 均检测到 **1 次树根移动、第 2 轮收敛**；快照被中途驱逐（404）同样整轮重试成功 |
| 删除墓碑 | 墓碑随桶复制，对端该键变 404；再次同步 0 桶 0 记录 |
| 同键并发写冲突 | 高 HLC 版本获胜，反向再同步状态不变（合并对称） |

### 7.2 扩展性（`go test -bench=BenchmarkSingleLeafSync`）

收敛后反复做反熵扫描，**稳态线网开销与数据量无关**（只是两次快照调用 +
比较两个根哈希）：

| 键数 | 稳态 wire B/run |
|---|---|
| 1,000 | 261 |
| 4,000 | 263 |
| 16,000 | 264 |

> 注意：当前快照是内存全量拷贝、建树是 O(n) CPU（16,000 键取快照约数十
> 毫秒）。这部分是本地成本，不产生网络传输；见第 9 节"未完成项"。

### 7.3 真实双进程端到端（curl）

两个 `merklekv` 进程，r1 写 key1..100、r2 写 key50..149（key50..100 同键
不同值）：

- 首次同步：收敛为 149 键并集，1 轮、0 次冲突；
- 第二次同步：`buckets_opened=0, entries_exchanged=0, wire=259 B`；
- 在 r2 单改 key7 后同步：`hash_nodes=4, buckets=1, entries=1, wire=4498 B`
  （全量基线 26,912 B），r1 读到 `CHANGED-VALUE`；
- 在 r2 删除 key50 后同步：r1 对 key50 返回 404，墓碑计数为 1。

---

## 8. 运行测试

```bash
make test      # go test -count=1 ./...
make race      # go test -race -count=1 ./...
make bench     # 基准测试
make check     # gofmt 检查 + vet + 测试
```

测试构成：

- `internal/hlc`：时钟单调、墙钟回退、Observe 前推、全序比较；
- `internal/store`：墓碑、epoch 守卫拒绝陈旧提交、LWW/墓碑/复活/同时间戳决胜、
  快照不可变性；
- `internal/merkle`：分区稳定性、同内容同根、单叶变化只动一条路径、
  `ExpectedRoot` 预测、墓碑/版本入哈希、路径导航边界；
- `internal/server`：快照/节点/桶记录往返、404 快照、409 epoch、400 参数、
  旧版本忽略、坏 JSON；
- `internal/sync`：第 7.1 节全部验收场景 + 幂等 + HTTP KV 冒烟。

---

## 9. 未完成项 / 已知局限

如实记录当前版本没有做的部分：

1. **无持久化**。状态只在内存，进程退出即丢；快照也是内存拷贝。接入
   LSM/BoltDB 之类存储后，快照可用 MVCC 引用替代全量拷贝。
2. **建快照/建树是 O(n) CPU**。树在每个快照上全量构建并缓存；网络交换是
   O(差异)，但本地计算尚未增量维护。可改为后台增量重算脏桶。
3. **墓碑不垃圾回收**。删除记录永久保留并参与哈希。多副本长期运行需要
   安全的墓碑压缩协议（带副本确认水位），未实现。
4. **同步是成对的、由一方主动发起**；没有 gossip 调度、成员管理、周期
   自动后台反熵（`/v1/sync` 可被外部定时器调用，效果等同）。
5. **桶数启动时固定**（默认 4096）。极端键量下热点桶会变大；没有自适应
   再分区（这也是"固定分区"刻意的简单性取舍）。
6. **无鉴权/TLS**，默认只监听 127.0.0.1；公网部署需加反向隧道或中间件。
7. 时钟依赖本机墙钟（毫秒）。HLC 能吸收小幅回退/漂移，但不防蓄意乱改时钟。
8. `/v1/admin/seed` 与 `/v1/debug/*` 是测试/演示接口，未做权限隔离。
