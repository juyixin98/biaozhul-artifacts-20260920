# OR-Set 复制服务（Observed-Remove Set CRDT）

纯后端、零第三方依赖的 **OR-Set（观察删除集合）** 多副本复制服务。仅使用 Go
标准库 `net/http`，提供状态复制与操作复制两种通道，支持离线副本、消息任意
重排/重复投递，并实现安全的墓碑回收。

---

## 1. 它解决什么问题

普通"删除"在分布式多副本下有二义性：副本 A 删 `x` 的同时副本 B 重新加入
`x`，网络分区愈合后到底该不该有 `x`？

OR-Set 的规则：

- 每次 **add** 都生成一个**全局唯一标签（tag）**挂在元素上；
- 元素是成员 ⇔ 它至少有一个**未被墓碑化**的标签；
- **remove 只把删除方当前已观察到的标签**写入墓碑集（tombstone）；
- 因此与删除并发的 add 用的是删除方从未见过的新标签，**add 胜出**，不会被误删。

复制：

- 复制 = 对 add 映射和墓碑集做**集合并（join）**；
- 并集满足**交换律、结合律、幂等律**（join-semilattice），所以：
  任意消息顺序、重复同步、丢消息后重试、副本长期离线，**最终都收敛到同一集合**。

---

## 2. 目录结构

```
.
├── go.mod                     # 模块定义；无第三方依赖（依赖即被锁定）
├── Makefile
├── cmd/
│   ├── server/main.go         # 副本服务进程（HTTP API）
│   └── gc/main.go             # 墓碑回收协调器 CLI
├── internal/
│   ├── orset/orset.go         # CRDT 核心：唯一标签 / 观察删除 / 合并 / 回收
│   ├── orset/orset_test.go    # 排列测试、随机重排、竞态、半格定律
│   ├── gc/gc.go               # 安全回收判定与协调（交集 + fail-closed）
│   ├── gc/gc_test.go
│   ├── server/server.go       # net/http 接口层
│   └── server/server_test.go  # 三副本分区合并 6 种愈合顺序、GC 端到端
├── scripts/demo.sh            # 三副本完整演示脚本
└── docs/test-output.txt       # 实际测试输出存档
```

## 3. 依赖

- Go ≥ 1.22（开发实测版本 **go1.23.4 linux/amd64**，用到 1.22 的
  `mux.HandleFunc("POST /path")` 方法路由）。
- 运行/测试只用到标准库（`net/http`、`crypto/rand`、`encoding/json`、`sync` 等）。
- **没有任何第三方依赖，因此没有也不需要 `go.sum`**；`go.mod` 本身就是完整、
  可复现的依赖锁定。需要可离线构建时可执行 `go mod vendor`（生成的 vendor
  目录为空占位，全部来自标准库）。
- 演示脚本需要 `curl`。

## 4. 启动

```bash
# 编译
go build ./...

# 启动一个副本（默认 :8080，副本 id 默认 主机名-pid）
go run ./cmd/server -addr :8081 -id node1
go run ./cmd/server -addr :8082 -id node2   # 另开终端
go run ./cmd/server -addr :8083 -id node3   # 另开终端
```

健康检查：

```bash
curl -s http://127.0.0.1:8081/health
# {"replicaId":"node1","status":"ok"}
```

一键三副本演示（自动起 3 个进程、制造分区、愈合、GC）：

```bash
make demo          # 或 ./scripts/demo.sh
```

## 5. HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 存活与副本 id |
| GET | `/` | 接口清单 |
| POST | `/elements` | add，body `{"element":"x"}`，返回新铸造标签与可复制的 op |
| DELETE | `/elements/{e}` | observed-remove；未观察到任何标签时返回 204 |
| GET | `/elements` | 当前成员 `{"elements":[...]}` |
| GET | `/state` | 完整 CRDT 状态（状态复制拉取） |
| POST | `/state` | 合并外部状态（状态复制推送） |
| POST | `/ops` | 应用/复制单个 op（操作复制） |
| POST | `/sync` | 与 peers 交换状态，body `{"peers":[...],"pull":true,"push":true}` |
| GET | `/gc/eligible?peer=&peer=` | 回收**预演**：跨所有列出副本求可安全回收交集 |
| POST | `/gc` | 协调一次完整回收（含本副本） |
| POST | `/gc/purge` | 管理接口：直接按标签物理删除 |
| GET | `/debug/stats` | 成员数 / add 标签数 / 墓碑标签数 |

所有请求/响应均为 JSON，请求体上限 4 MiB，未知字段返回 400。

### 请求样例

```bash
B=http://127.0.0.1:8081

# 加入 x（注意返回的唯一 tag）
curl -s -XPOST $B/elements -H 'Content-Type: application/json' -d '{"element":"x"}'

# 查看集合
curl -s $B/elements

# 删除 x（仅墓碑化该副本已观察到的标签）
curl -s -XDELETE $B/elements/x

# 状态复制：把 node1 的完整状态推给 node2 合并
curl -s $B/state > /tmp/s1.json
curl -s -XPOST http://127.0.0.1:8082/state \
  -H 'Content-Type: application/json' \
  -d "{\"state\":$(jq '.state' /tmp/s1.json)}"

# 操作复制：单条 op（幂等、可乱序、可重复）
curl -s -XPOST $B/ops -H 'Content-Type: application/json' \
  -d '{"type":"add","element":"y","tags":["node1-9-9f.."]}'

# 让 node1 与 node2、node3 做一次双向（拉+推）状态交换
curl -s -XPOST $B/sync -H 'Content-Type: application/json' \
  -d '{"peers":["http://127.0.0.1:8082","http://127.0.0.1:8083"]}'
```

## 6. 离线副本与任意消息重排，为什么一定收敛

- **唯一标签**：`<replicaID>-<本副本单调计数器>-<16 字节 crypto/rand>`，
  跨重启、跨机器都不会碰撞（见 `internal/orset/orset.go` 的 `mintTag`）。
- **状态通道（`Merge`）**：只是两个 map 的集合并。重复合并是幂等的，乱序
  合并由交换/结合律保证结果相同。
- **操作通道（`ApplyOp`）**：remove 会把它携带的标签同时写入 add 历史与墓碑
  集。这样**即使 remove 先于对应的 add 到达**，等 add 之后到来时标签早已是
  死的，元素不会"复活"；真正的新 add 仍正常生效。op 天然幂等。
- 这两条保证在测试中被穷举/随机验证（见下）。

## 7. 墓碑何时可安全回收（GC）

墓碑**不能在单副本本地删除**：一个还没收到对应 add 的离线副本，日后收到该
add 时若墓碑已不存在，就会让已删除元素复活。

一个标签 `T` 的墓碑可在副本 `R` 物理删除，**当且仅当**：

1. **每一个仍然存活的副本**都已经同时观察到 `T` 的 add 与 tombstone（即所有
   副本都已把 `T` 解析为死）；
2. 永久下线（已退役/磁盘销毁）的副本可排除在仲裁之外，但**仅仅离线、慢、
   分区中的副本必须计入**——它们积压的消息里可能正带着 `T`。

本实现（`internal/gc`）的做法：

- 协调器拉取**所有**在役副本的状态，取墓碑集**交集**，并保守地要求该 add
  标签也在每个副本的 add 历史中出现；交集内的标签才允许回收（`Eligible`）。
- 任一副本状态拉取失败 → **本轮在任何 purge 之前中止（fail-closed）**，宁可
  多留墓碑也不冒险复活。
- purge 自身幂等，可安全重试。

```bash
# 预演（只计算，不删除）
curl -s "$B/gc/eligible?peer=http://127.0.0.1:8082&peer=http://127.0.0.1:8083"

# 正式协调回收（经任一副本的 API，会同时清理本副本与 peers）
curl -s -XPOST $B/gc -H 'Content-Type: application/json' \
  -d '{"peers":["http://127.0.0.1:8082","http://127.0.0.1:8083"]}'

# 或使用独立 CLI
go run ./cmd/gc -peers http://127.0.0.1:8081,http://127.0.0.1:8082,http://127.0.0.1:8083
```

> 注意：生产环境还需要一个"成员资格/退役（membership）"事实来源来确认哪些
> 副本算"永久下线"。本实现把参与仲裁的副本集合作为每次调用的显式参数，
> 未内置成员资格服务（见"未完成/局限"）。

## 8. 自动化测试与验收

```bash
make test          # go test ./...
make test-race     # go test -race -count=1 ./...（竞态检测）
make demo          # 三副本端到端演示
```

测试覆盖（对应验收项）：

1. **并发增加/删除**
   - `TestRemoveOnlyRemovesObserved`：分区后删除方只能墓碑已观察标签，并发
     add 的新标签存活（add-wins）。
   - `TestAddWinsConcurrentRemove`：教科书式 add/remove 并发，合并后元素仍在。
   - `TestConcurrentAddRemoveMerge`：8 个 goroutine 并发 add/remove + 2 个
     goroutine 并发合并快照，`-race` 下跑，结束后各副本状态一致。
2. **重复同步**
   - `TestIdempotentMergeAndOps`：同状态合并 5 次、同 op 投递 5 次结果不变；
     演示脚本第 4 步对重复全量同步前后状态做字节级比对。
3. **三副本分区合并**
   - `TestThreeReplicaPartitionHeal`：三副本先共同历史、再分区各自修改
     （含 add/remove 冲突），对**全部 6 种愈合顺序**断言最终成员一致且 CRDT
     状态逐副本相等，随后两轮重复同步不得改变状态。
4. **排列测试证明收敛**
   - `TestOpDeliveryPermutationsExhaustive`：对 7 个并发 op 的 **7! = 5040
     种投递顺序全部穷举**，每种还额外整序重放一遍，结果必须等于同一不动点。
   - `TestOpDeliveryRandomReorders`：三副本分区历史，500 次随机乱序 + ~30%
     消息重复，3 个接收方各自收敛到同一状态。
   - `TestStateDeliveryRandomReorders`：500 次状态快照乱序/重复/跳序合并。
   - `TestSemilatticeLaws`：直接验证合并的交换律、结合律、幂等律。
   - `TestRemoveBeforeAddArrives`：remove 先于 add 到达仍稳定，不复活。
5. **墓碑回收**
   - `TestEligibleIntersection` / `TestEligibleEmpty`：交集判定；全新副本一票否决。
   - `TestCoordinateFailsClosedOnStateFetch`：任一副本不可达时零 purge。
   - `TestCoordinatePurgesEverywhere`：交集标签各节点清除、未观察标签保留、
     二次回收为空（幂等）。
   - `TestGCEndToEnd`：经真实 HTTP 完成收敛→预演→回收，计数归零；未同步的
     第 4 副本使可回收数为 0。

实测结果存档于 `docs/test-output.txt`（`go test -race ./... -v` + `go vet` +
`gofmt` 全部通过）。演示脚本实测：三副本最终均为 `[a c d e]`（并发 add 的
`a` 胜出、被所有副本观察删除的 `b` 消失）；GC 前 `tombstoneTags=2`，协调回收
后三节点均为 `0` 且成员不变；引入未同步第 4 副本后 `eligibleCount=0`。

## 9. 未完成 / 已知局限

- **持久化**：状态仅在内存，进程重启即丢失。生产上应把 Snapshot/Op 落
  WAL；当前刻意保持范围最小。
- **成员资格/退役服务**：GC 仲裁的副本集合需调用方显式给出，未内置
  membership；永久下线节点需要外部权威确认后才能排除。
- **无鉴权/TLS**：接口默认明文、无认证，仅适合本地/可信网络演示。
- **非增量同步**：`/state` 与 `/sync` 交换全量状态，未做版本向量/增量摘要，
  大集合下流量与状态规模成正比。
- 无前端界面（按要求仅纯后端）。
