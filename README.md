# vcreg — 向量时钟多版本寄存器

用 Go 标准库 `net/http` 实现的纯后端模拟：一个进程内运行多个**互不共享状态**的副本
（replica），副本之间只能通过显式的 HTTP 同步接口交换消息，等价于真实网络。
每个 key 保存的是一个**多版本寄存器**：

- 每次写入附带一个**向量时钟**（vector clock）；
- 时钟构成 happens-before（先于）关系：`a ≤ b` 当且仅当 a 每个分量都 ≤ b；
- **因果覆盖**：新写的时钟支配旧写 → 旧版本被淘汰；
- **并发写**：两个时钟互不支配 → 二者作为 **sibling（兄弟版本）同时保留**，不做静默 last-write-wins；
- **显式合并**：调用方带着 `context`（它实际看到的兄弟时钟）发起 resolve，
  服务端校验这些时钟确实是当前兄弟，然后产生一个后继版本（时钟为上下文的 join +1），
  仅淘汰上下文中的兄弟；上下文之外的并发版本一律保留。

合并语义是基于状态的 join（幂等、可交换、可结合），因此**消息重复、乱序都不改变最终结果**。

## 目录结构

```
go.mod                          # 模块定义；零第三方依赖，无 go.sum
cmd/vcregd/main.go              # HTTP 服务入口
internal/vclock/vclock.go       # 向量时钟：Compare/Join/Increment/规范化
internal/register/register.go   # 多版本寄存器、Store、快照合并、resolve
internal/server/server.go       # net/http 路由与 JSON 协议
internal/*_test.go              # 单元 + HTTP 端到端测试（含全部验收场景）
```

## 依赖与环境

- Go ≥ 1.23（开发实测版本：`go1.23.4 linux/amd64`）。
- **仅使用 Go 标准库，没有任何第三方依赖**，因此 `go.mod` 本身即完整、可复现的依赖锁定
  （没有 `go.sum`；无外部模块需要校验和）。
- 默认监听 `:8080`。

## 启动

```bash
go run ./cmd/vcregd -addr :8080 -replicas r1,r2,r3
# 或先编译
go build -o vcregd ./cmd/vcregd
./vcregd -addr :8080 -replicas r1,r2,r3
```

启动后可 `GET /` 查看路由清单，`GET /replicas` 查看副本。副本也可以运行中用
`PUT /replicas/{id}` 动态创建。

## HTTP 接口与请求样例

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/replicas` | 列出副本 |
| PUT | `/replicas/{rid}` | 创建副本 |
| GET | `/replicas/{rid}/keys` | 列出有数据的 key |
| GET | `/replicas/{rid}/keys/{key}` | 读取 key 的全部兄弟版本 |
| PUT | `/replicas/{rid}/keys/{key}` | 本地写；body `{"value":"..."}`，可选 `clock` 作为显式父时钟 |
| POST | `/replicas/{rid}/keys/{key}/resolve` | **带上下文的显式合并** |
| POST | `/replicas/{rid}/receive/{key}` | 投递单条版本消息（可重复投递） |
| POST | `/sync` | 快照同步 `{"from":"r2","to":"r1"}`：把 from 全量并入 to |

写入语义：不传 `clock` 时，父时钟自动取该副本当前可见兄弟时钟的 join
（因此同一副本上的连续写构成因果链）；显式传 `clock`（含空对象 `{}`）则按给定上下文写，
可用于制造刻意的盲写/并发。

### 1. 隔离双写后同步（保留并发版本并收敛）

```bash
# 分区：双方互不可见，各自写
curl -X PUT localhost:8080/replicas/r1/keys/cfg -d '{"value":"red"}'
# {"status":"written","version":{"value":"red","clock":{"r1":1},"origin":"r1"}}
curl -X PUT localhost:8080/replicas/r2/keys/cfg -d '{"value":"blue"}'
# {"status":"written","version":{"value":"blue","clock":{"r2":1},"origin":"r2"}}

# 愈合：双向同步
curl -X POST localhost:8080/sync -d '{"from":"r2","to":"r1"}'
curl -X POST localhost:8080/sync -d '{"from":"r1","to":"r2"}'

curl localhost:8080/replicas/r1/keys/cfg
# {"count":2, ... ,"siblings":[
#   {"value":"red","clock":{"r1":1},"origin":"r1"},
#   {"value":"blue","clock":{"r2":1},"origin":"r2"}]}
```

`{r1:1}` 与 `{r2:1}` 互不支配（concurrent），两个版本都保留；r1、r2 状态一致 = 收敛。

### 2. 重复消息（幂等）

```bash
MSG='{"value":"blue","clock":{"r2":1},"origin":"r2"}'
curl -X POST localhost:8080/replicas/r3/receive/cfg -d "$MSG"
# {"admitted":true, "status":"admitted", ...}
curl -X POST localhost:8080/replicas/r3/receive/cfg -d "$MSG"
# {"admitted":false,"status":"ignored_stale_or_duplicate", ...}

curl -X POST localhost:8080/sync -d '{"from":"r2","to":"r3"}'  # versions_admitted:0
```

### 3. 带上下文的显式合并，以及合并后迟到旧写

```bash
# 先 GET 拿到两个兄弟时钟，作为 context 提交
curl -X POST localhost:8080/replicas/r1/keys/cfg/resolve -d '{
  "value":"purple",
  "context":[{"r1":1},{"r2":1}]}'
# {"status":"resolved","version":{"value":"purple","clock":{"r1":2,"r2":1}, ...}}

# 传播合并结果
curl -X POST localhost:8080/sync -d '{"from":"r1","to":"r2"}'

# 合并前的旧写迟到/重投：时钟被合并版本支配 -> 丢弃，不复活兄弟
curl -X POST localhost:8080/replicas/r2/receive/cfg \
  -d '{"value":"blue","clock":{"r2":1},"origin":"r2"}'
# {"admitted":false,"status":"ignored_stale_or_duplicate",
#  "siblings":[{"value":"purple","clock":{"r1":2,"r2":1}, ...}]}
```

上下文校验：引用一个并非当前兄弟的时钟会得到 `409 Conflict`
（`context clock is not a current sibling of the key`），防止在过期观察上合并。

### 4. 合并不会误删上下文之外的并发版本

三个并发兄弟 sun/rain/fog，只把 sun、rain 列入 context 合并成 storm：
fog 与新时钟仍并发，**继续保留**：

```bash
curl -X POST .../weather/resolve -d '{"value":"storm","context":[{"r1":1},{"r2":1}]}'
# 存活版本：["storm","fog"]
```

## 测试

```bash
go vet ./...
go test -race -count=1 ./...
```

- `internal/vclock`：先于/并发判定、join、increment、规范化。
- `internal/register`：直接覆盖三条验收场景
  （`TestPartitionedDoubleWriteThenSync`、`TestDuplicateMessage`、
  `TestLateStaleAfterMerge`），外加因果覆盖、上下文外兄弟保留
  （`TestResolveKeepsUnrelatedConcurrent`）、合并顺序无关、三副本收敛、上下文校验。
- `internal/server`：通过 `httptest` 走真实 HTTP 路由的端到端测试
  （分区→同步→resolve→迟到旧写；重复投递；409/404）。

### 实测结果（2026-09-24，go1.23.4，linux/amd64）

```
$ go vet ./...        # 无输出（通过）
$ go test -race -count=1 ./...
?  	vcreg/cmd/vcregd	[no test files]
ok 	vcreg/internal/register	1.015s
ok 	vcreg/internal/server	1.033s
ok 	vcreg/internal/vclock	1.015s
```

另用真实启动的服务（`./vcregd -addr :18080`）+ curl 手工跑过全部四个场景，
输出与上述样例一致：隔离双写后两副本各有 `red+blue`；重复单消息/重复快照
`admitted=false / versions_admitted=0`；resolve 后只剩 `purple`（`{r1:2,r2:1}`），
迟到的 `red@r1:1`、`blue@r2:1` 均被拒绝；局部合并时未列入 context 的 `fog` 保留。

## 设计说明与边界

- 每个 key 的存活版本集合始终是向量时钟偏序上的一个**反链**（antichain）：
  不存在一个存活版本支配另一个。并入新版本就是在反链上做 join。
- 时钟相等视为同一逻辑事件（重复投递丢弃，先到者保留）。
- 这是**状态复制**模拟：没有真实网络/持久化/磁盘日志；进程退出数据即丢失，
  同步是整份快照（可扩展为增量，接口已足够）。
- 未做：鉴权、TLS、持久化、删除（tombstone）、跨 key 事务——本任务范围内有意省略。
