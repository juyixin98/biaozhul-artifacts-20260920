# merklesync — 反熵 Merkle 树键值同步

用固定分区 Merkle 树在两个键值副本之间定位差异的纯后端反熵（anti-entropy）同步服务。
只依赖 Go 标准库（`net/http`、`crypto/sha256`、`encoding/json`），无任何第三方依赖、无界面、无持久化。

## 能力

- **固定分区 Merkle 树**：键空间按哈希固定切分为 16 个桶（叶子），其上是 8/4/2/1 的完整二叉树（共 5 层）。
  两副本从根开始比对，只沿哈希不同的子树下降，最终只定位到真正变化的桶。
- **版本比较 + 删除墓碑（tombstone）**：每条记录带单调递增版本号，按 LWW（版本号大者胜）合并；
  删除写入带版本的墓碑，墓碑也参与哈希并向对端传播；删除一个本端不存在的键也会留下墓碑，以便传播给仍持有该键的副本。
- **一致快照 + 扫描中变更检测**：每个带数据的扫描请求都携带轮次开始时的 `revision`；
  服务端若发现该 revision 已过期（扫描期间有写入提交），返回 **HTTP 409**，客户端整轮丢弃并从新的 `/root` 重试，最多 20 轮。
- **稀疏传输**：桶内先只交换 `(key, version, deleted)` 元数据；仅当对端版本更新且非墓碑时，才拉取真正的 value。
  已收敛时再次同步只交换一个根哈希。
- 双向：一轮内同时从 peer 拉取较新记录、向 peer 推送本地较新记录。

## 目录结构

```
go.mod
cmd/merklesync/        CLI：serve / sync / demo
  main.go              HTTP 服务端启动、sync 子命令
  demo.go              内置验收演示（三个场景）
internal/
  store/               版本化内存 KV、墓碑、revision/epoch
  merkle/              固定 16 分区 Merkle 树（FNV 分桶 + SHA-256）
  syncapi/
    server.go          HTTP handler、revision 校验、409、扫描期注入钩子
    client.go          JSON/HTTP 客户端 + 流量统计
    sync.go            反熵同步器（树下降 / 元数据比较 / 值拉取 / 409 重试）
    local.go           本地副本抽象（进程内 store 或 HTTP 远端）
```

## 依赖

- Go 1.23+（在 go1.23.4 linux/amd64 上开发与测试）。
- 零第三方依赖；`go.mod` 不 require 任何外部模块（即"锁定依赖"：没有需要下载的东西）。

## 构建与测试

```bash
go build ./...
go vet ./...
gofmt -l .          # 应无输出
go test ./...       # 单元 + 端到端测试（httptest）
go test -race ./... # 带竞态检测
```

## 启动命令

### 1) 启动副本

```bash
go run ./cmd/merklesync serve -addr :8080
# 或
go build -o bin/merklesync ./cmd/merklesync
./bin/merklesync serve -addr 127.0.0.1:8080
```

数据全在内存，重启即空。

### 2) 同步两个已运行的副本（双向收敛）

```bash
./bin/merklesync sync -local http://127.0.0.1:8080 -peer http://127.0.0.1:9090
```

输出 JSON 统计：轮次、冲突数、定位到的差异桶、比较的哈希/元数据数、实际拉取的值数、
推送条数、墓碑数，以及线字节数（`bytesSent/bytesReceived`）。

### 3) 内置验收演示（自动起两个内存副本，无需手动起服务）

```bash
./bin/merklesync demo          # 默认每副本 200 个种子键
./bin/merklesync demo -seed 500
```

三个场景：①单叶变化 ②双向全量差异（更新/删除/新增）③扫描期间 peer 被写入（409 + 重试）。
机器可读结果额外写入运行目录下的 `demo-results.json`。

## HTTP 接口

所有 body 均为 JSON。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET  | `/root` | 当前 `{epoch, revision, root}` |
| POST | `/nodes` | 给定 `revision`、`level`、`indexes[]`，返回对应节点哈希 |
| POST | `/leaves` | 给定 `revision`、`buckets[]`，返回桶内记录元数据 `(key,version,deleted)`（不含 value） |
| POST | `/entries` | 给定 `revision`、`keys[]`，返回完整记录（含 value） |
| POST | `/apply` | 批量 LWW 合入记录（含墓碑），返回实际生效条数；**不校验 revision**（旧版本天然输掉） |
| GET  | `/snapshot` | 全量快照（朴素基线/运维用，同步器不用） |
| POST | `/put` | 写 `{key,value,version}`（version=0 表示自动 +1） |
| POST | `/delete` | 写墓碑 `{key,version}` |
| POST | `/reset` | 清空并进入新 epoch |
| POST | `/debug/chaos` | 测试钩子：在扫描中注入一次（或重复）写入/删除 |

revision 过期时，`/nodes`、`/leaves`、`/entries` 返回：

```json
HTTP 409
{"error":"revision changed during scan; retry with a fresh root","currentRevision":35,"currentEpoch":0}
```

### curl 请求样例

```bash
A=http://127.0.0.1:8080
B=http://127.0.0.1:9090

# 写入 / 删除
curl -s -XPOST $A/put    -d '{"key":"k1","value":"hello","version":1}'
curl -s -XPOST $A/delete -d '{"key":"k2","version":2}'

# 看根
curl -s $A/root
curl -s $B/root

# 取第 3 层两个子节点哈希（同步器第一步）
curl -s -XPOST $B/nodes -d '{"revision":34,"level":3,"indexes":[0,1]}'

# 取 3、7 号桶的键元数据（不含 value）
curl -s -XPOST $B/leaves -d '{"revision":34,"buckets":[3,7]}'

# 只拉需要的完整值
curl -s -XPOST $B/entries -d '{"revision":34,"keys":["k1"]}'

# 直接批量合入
curl -s -XPOST $A/apply -d '{"entries":[{"key":"k1","value":"x","version":2}]}'

# 注入"扫描中写入"：下一轮第一次扫描读之前把 k9 改为 v5（repeat=2 可连续制造 3 轮冲突）
curl -s -XPOST $B/debug/chaos -d '{"after":0,"op":"put","key":"k9","value":"late","version":5}'
```

## 同步算法（一轮）

1. 取本地快照建树；`GET /root` 取 peer 的 `(revision, root)`。根相同 → 本轮直接结束。
2. 自第 3 层（根的两个孩子）逐层向下批量取节点哈希；只把哈希不同的节点的孩子加入下一层；
   比较一直推进到第 0 层，因此一个内部节点不同不会让我们去扫未变化的兄弟桶。
3. 对差异桶调 `/leaves` 取键元数据，在本地按 key 对齐：
   - 对端版本新且存活 → 记入"待拉值"；对端版本新但是墓碑 → 直接本地合入墓碑；
   - 本地版本新 → 稍后批量推送；任一侧缺失的键按存在方处理。
4. 仅对待拉值的键调一次 `/entries` 批量取值，再做一次版本校验后本地 LWW 合入。
5. 本地较新记录（含墓碑、对端缺失键）一次 `/apply` 批量推送。
6. 任一步收到 409 → 丢弃本轮、重新取根重试。
7. 轮末双方重新取根校验；一致才算 `converged`，否则继续。

## 实际运行结果（如实记录）

环境：go1.23.4 linux/amd64。以下数字由 `./bin/merklesync demo`（默认 200 个种子键，
每个 value 100 字节）实跑产生。

**场景 1（单叶变化，200 个共享键中 peer 改 1 个）**

- 朴素全量拉取（`GET /snapshot`）：接收 **28356 字节**。
- Merkle 同步：`differingBuckets=1`、比较 8 个节点哈希、12 条桶内元数据，
  **实际只拉取 1 个 value**；线接收 **1249 字节**、发送 234 字节；`attempts=1`，一次收敛。
- 收敛后再次同步：`attempts=1`、`nodeHashesCompared=0`、零值传输（只换根）。

**场景 2（双向全量差异）**：peer 更新 10 键(v2)、删除 5 键(墓碑 v2)；本地更新另 10 键(v2)、新增 7 键。

- `differingBuckets=16`（变化恰好覆盖全部 16 个桶）、200 条元数据，
  但**只拉取 10 个 value、推送 17 条**，`tombstonesExchanged=5`，一次收敛，双方根一致。
- 线接收 9142 字节 vs 朴素全量 27099 字节。广泛分歧时元数据会全扫（结构性必然），
  但 value 仍只传真正变化的。

**场景 3（扫描期间更新）**：扫描前 peer 有 1 个 v2 差异，并通过 `/debug/chaos`
在第一轮第一次 `/nodes` 之前再提交 1 个写入。

- `attempts=2, conflicts=1`：第一轮在该 `/nodes` 处收到 409 被丢弃，取新根重试后收敛；
  两个更新（扫描前 + 扫描中）都未丢失，`valuesPulled=2`，最终双方根一致。
- 另有压力测试 `TestSyncConvergesAcrossMultipleScanTimeWrites`：连续 3 轮扫描都撞上写入
  （`repeat=2`），得到 `conflicts=3, attempts=4`，第 4 轮静默期收敛，最终值版本正确。

**两真实服务器 + CLI（curl 实测）**：30 个共享键上制造 4 处差异
（A 更新 1 键到 v2、A 新增 1 键、B 删除 1 键(墓碑)、B 新增 1 键），
`merklesync sync` 一次收敛：`differingBuckets=3, valuesPulled=1, entriesPushed=2, tombstonesExchanged=1`，
同步后两服务 root 相同；直接对旧 revision 调 `/nodes` 返回 `HTTP 409` 及 `currentRevision`。

**测试**：`go test -race -count=1 ./...` 全部通过（store 5 个、merkle 5 个、syncapi 10 个测试用例）。
覆盖：LWW 胜负、墓碑传播与删除不存在键、版本/值/删除各自改变根、单键变化只翻转一条根路径、
插入顺序无关、空操作短路、双向收敛、反向空操作、扫描中更新/删除的 409 重试、多轮冲突收敛、
完全不相交键空间、稀疏传输字节上界。

## 设计取舍与边界

- 固定 16 桶、4 层二叉：树很小（全部节点哈希也就 31 个），层数固定，请求轮数有界（每个 level 一次批量请求）。
  代价是数据量极大时单桶可能很大；这是"固定分区"题设下的有意取舍。
- 版本号由写者提供/自动递增，采用 LWW；没有向量时钟，因此**并发写同一版本号**按"先到者保留"处理
  （`<=` 判负），不解决同版本冲突的语义合并。
- 墓碑永久保留（无 GC / 无存活时间），长期运行下墓碑集合会单调增长。
- 内存存储，无持久化、无鉴权、TLS；`/debug/chaos` 是测试钩子，生产部署应移除或限制访问。
- 同步器是发起方拉/推；`/apply` 不做 revision 门控，因为 LWW 下旧记录幂等且必败。
- 持续高速写入（每轮都撞上提交）超过 20 轮时返回 `ErrNotConverged`，不做退避之外的协调。

## 未完成 / 未做项

- 无持久化（重启丢数据）——题设只要求内存定位服务，未引入存储引擎。
- 墓碑 GC、向量时钟/多副本因果、鉴权与 TLS、可观测性指标（metrics）均未实现。
- 树规模固定（16 桶），未做可配置分桶或动态再分区。
- 仅有两个副本的同步；多于两方的拓扑（星型/环形 gossip）未编排，但任两副本间都可直接跑本同步器。
