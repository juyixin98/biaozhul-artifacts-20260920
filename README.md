# 分片迁移切换协议模拟器（Go / net/http，纯后端）

单主分片（single-primary shard）迁移的内存模拟器，把真实系统中
“旧主 → 新主”的在线迁移拆成三个**显式、可重放、可审计**的阶段，并提供 HTTP
接口。仅依赖 Go 标准库（`net/http` + `encoding/json`），**零第三方依赖**。

- 语言：Go 1.23（`go.mod` 声明 `go 1.23`，用到 Go 1.22 的 ServeMux 方法路由）
- 依赖：无第三方包，因此没有 `go.sum`（`go mod tidy` 后也为空）
- 持久化：无，进程内存态；重启即重置

## 它保证什么（核心不变量）

1. **每次写有序号**：每个分片内单调递增 `seq = 1..N`，失败/被拒绝的写不占号。
2. **切换点前后只能有一个主确认新写**：存在一个明确的 `switch_seq` 边界
   - `seq <= switch_seq` 的写全部由旧主（`node-a`，路由 v1）确认；
   - `seq >  switch_seq` 的写全部由新主（`node-b`，路由 v2）确认；
   - 旧主在切换后、新主在切换前，确认数都必须为 0。
3. **路由版本防双主**：切换把路由版本 v1→v2。仍持 v1 的旧客户端写一律被拒
   （`409 stale_route`），即使旧主仍在线也不会再次确认写。
4. **已确认写不丢**：切换是原子的，且只有当新主**实际持有全部已确认写**
   （以每条写的 `delivered_to` 为准，而非仅看水位线）时才允许切换；
   目标断连或仍有缺口时切换被拒（`409 target_disconnected / lag_remaining`）。
5. **控制消息可幂等重放**：重复的 START / SNAPSHOT_COMPLETE / CATCHUP / SWITCH
   带相同 `idempotency_key` 时返回首次结果（`idempotent_replay: true`），
   不产生第二次状态变更；同 key 用于不同动作判为冲突 `409 idempotency_conflict`。

## 三阶段协议

```
RUNNING ──START──▶ SNAPSHOT ──SNAPSHOT_COMPLETE──▶ CATCHUP ──SWITCH──▶ SWITCHED
                     │                                   │
                 旧主仍接受写                        新写实时流式到新主
                 (seq>start_seq                      新主断连→该写未投递，
                  走增量通道)                         重连后 CATCHUP 补齐
```

| 阶段 | 旧主 node-a | 新主 node-b | 路由 |
|---|---|---|---|
| RUNNING | 接受写 | 副本 | v1 |
| SNAPSHOT | 仍接受写（不停写） | 接收 `seq<=start_seq` 全量快照 | v1 |
| CATCHUP | 接受写 | 新写实时增量；断连期间的写由 `catchup` 补 | v1 |
| SWITCHED | 不再确认写（v1 写被拒） | **唯一主**，接受写 | v2 |

切换门控：`SWITCH` 要求新主在线 **且** 不缺任何已确认写。该判定遍历写日志的
`delivered_to`，避免“水位线被后续写顶高、中间快照期写入漏投”的洞
（见 `internal/cluster/regression_test.go` 中针对该缺陷的回归测试）。

## 启动

```bash
# 需要 Go 1.23+
go version

# 直接运行（默认监听 :8080）
go run ./cmd/shardmigrator

# 或构建后运行，可用 -addr 或环境变量 ADDR 改端口
go build -o shardmigrator ./cmd/shardmigrator
ADDR=:8080 ./shardmigrator
```

启动后内置：节点 `node-a`（主）、`node-b`（副本），分片 `orders`（成员
`[node-a, node-b]`，初始主 node-a，路由 v1）。

健康检查：

```bash
curl -s localhost:8080/healthz
# {"ok":true,"data":{"status":"ok"}}
```

## HTTP 接口

所有响应为统一信封：`{"ok":bool,"data":...}` 或 `{"ok":false,"error_code":...,"error":...}`。

| 方法 & 路径 | 作用 |
|---|---|
| `GET  /healthz` | 健康检查 |
| `GET  /state` | 整个集群快照（节点、各分片、路由版本、写日志） |
| `GET  /shards/{id}` | 单分片状态 |
| `GET  /shards/{id}/writes` | 已确认写日志（含 `delivered_to`） |
| `GET  /shards/{id}/audit` | 安全不变量审计（验收用） |
| `POST /shards/{id}/writes` | 客户端写 |
| `POST /shards/{id}/migration/start` | RUNNING→SNAPSHOT |
| `POST /shards/{id}/migration/snapshot-complete` | SNAPSHOT→CATCHUP（装载快照） |
| `POST /shards/{id}/migration/catchup` | 增量补齐（新主断连恢复后） |
| `POST /shards/{id}/migration/switch` | CATCHUP→SWITCHED（原子切路由 v1→v2） |
| `POST /shards/{id}/reset` | 分片重置为初始 RUNNING（清写、清迁移态） |
| `POST /nodes/{id}/disconnect` | 模拟节点断连/网络分区 |
| `POST /nodes/{id}/reconnect` | 模拟节点恢复 |

错误码 → HTTP：`not_found` 404 / `stale_route`、`wrong_phase`、`target_disconnected`、
`lag_remaining`、`target_is_primary`、`idempotency_conflict`、`already_exists` 409 /
`primary_unavailable` 503 / `bad_request`、`bad_json` 400。

幂等键可放在请求体 `"idempotency_key"` 或请求头 `Idempotency-Key`（头优先）。
数据面写也支持幂等键，重放同一 key 返回同一个 `seq`。

## 请求样例

```bash
B=http://localhost:8080

# 1) 迁移前写入（路由 v1，旧主 node-a）
curl -s -X POST $B/shards/orders/writes \
  -H 'Content-Type: application/json' \
  -d '{"payload":"order-1","client_route_ver":1}'

# 2) 开始迁移 -> SNAPSHOT（带控制幂等键）
curl -s -X POST $B/shards/orders/migration/start \
  -d '{"idempotency_key":"k-start"}'
# 重复同 key 的 START：返回首次结果，"idempotent_replay":true，不重复迁移
curl -s -X POST $B/shards/orders/migration/start -d '{"idempotency_key":"k-start"}'

# 3) SNAPSHOT 阶段继续写（仍是旧主，序号增长但不停服务）
curl -s -X POST $B/shards/orders/writes -d '{"payload":"snap-write","client_route_ver":1}'

# 4) 模拟新主断连：快照完成必须失败（409）
curl -s -X POST $B/nodes/node-b/disconnect -d '{}'
curl -s -i -X POST $B/shards/orders/migration/snapshot-complete -d '{}'   # HTTP/1.1 409
curl -s -X POST $B/nodes/node-b/reconnect -d '{}'

# 5) 完成快照 -> CATCHUP（全量装载 + 顺带补快照期间增量）
curl -s -X POST $B/shards/orders/migration/snapshot-complete \
  -d '{"idempotency_key":"k-snap"}'

# 6) CATCHUP：新写实时流式；新主断连期间的写只在旧主确认，重连后补齐
curl -s -X POST $B/shards/orders/writes -d '{"payload":"catchup-ok","client_route_ver":1}'
curl -s -X POST $B/nodes/node-b/disconnect -d '{}'
curl -s -X POST $B/shards/orders/writes -d '{"payload":"catchup-missed","client_route_ver":1}'
curl -s -i -X POST $B/shards/orders/migration/switch -d '{}'             # 409 目标断连
curl -s -X POST $B/nodes/node-b/reconnect -d '{}'
curl -s -i -X POST $B/shards/orders/migration/switch -d '{}'             # 409 仍有缺口
curl -s -X POST $B/shards/orders/migration/catchup -d '{"idempotency_key":"k-cu"}'

# 7) 原子切换路由 v1 -> v2；重复 SWITCH 同 key 仅重放
curl -s -X POST $B/shards/orders/migration/switch -d '{"idempotency_key":"k-sw"}'
curl -s -X POST $B/shards/orders/migration/switch -d '{"idempotency_key":"k-sw"}'

# 8) 切换后旧路由 v1 一律拒绝（旧主在线也一样）-> 409 stale_route
curl -s -i -X POST $B/shards/orders/writes -d '{"payload":"stale","client_route_ver":1}'

# 9) 新主 node-b 在 v2 确认；即使此时旧主断连也正常
curl -s -X POST $B/nodes/node-a/disconnect -d '{}'
curl -s -X POST $B/shards/orders/writes -d '{"payload":"after-switch","client_route_ver":2}'
curl -s -X POST $B/nodes/node-a/reconnect -d '{}'

# 10) 验收审计
curl -s $B/shards/orders/audit
```

一键端到端演示脚本（内含写入、断连/重连、重复控制消息、审计）：

```bash
go run ./cmd/shardmigrator            # 终端 A
./scripts/demo.sh http://localhost:8080   # 终端 B
```

## 自动化测试 / 验收

```bash
go test ./...                  # 全部测试
go test -race ./...            # 竞态检测
go test -v -count=1 ./...      # 详细、不用缓存
go test -cover ./...           # 覆盖率
```

测试如何对应验收要求（在每个阶段插入写入、断连、重复控制消息）：

- `internal/httpapi/acceptance_test.go :: TestAcceptanceLifecycle`
  端到端走完三阶段；在 RUNNING/SNAPSHOT/CATCHUP/SWITCHED 各阶段都插入写；
  两次断连新主、一次断连旧主；重放全部四种控制消息；断言陈旧 v1 写被拒、
  新主 v2 独占确认、`seq 1..N` 稠密、审计 `healthy=true`。
- `internal/httpapi/concurrency_test.go :: TestConcurrentNoDualPrimary`
  切换瞬间让 v1、v2 两类客户端并发写，逐条校验边界两侧主身份/路由版本，
  并用 `-race` 反复跑，证明无数据竞争、无双主确认。
- `internal/httpapi/acceptance_test.go :: TestPrimaryDownRejectsWrites`
  主断连写返回 503 且**不消耗序号**。
- `internal/cluster/cluster_test.go` 阶段乱序控制消息、陈旧路由、未知目标、
  快照/增量投递、切换门控、reset。
- `internal/cluster/regression_test.go :: TestSnapshotPhaseWritesNoHole`
  专门回归“SNAPSHOT 阶段写被水位线漏投”的缺陷；
  `TestAuditRequiresFullDelivery` 校验缺口未补齐时禁止切换、审计报缺失。

审计字段（`GET /shards/orders/audit`）关键项，验收时应全为 0 且 `healthy=true`：
`missing_confirmed_writes`、`sequence_gaps`、`multi_confirmer_writes`、
`old_primary_confirms_after_switch`、`new_primary_confirms_before_switch`、
`stale_route_confirms`、`writes_missing_on_new_primary`。

## 实测结果（本机如实记录）

环境：Linux x86_64，`go version go1.23.4 linux/amd64`。

- `go vet ./...`：无输出（通过）；`gofmt -l .`：无输出。
- `go build ./...`：通过。
- `go test -race -count=3 ./...`：**9 个测试全部 PASS**（连跑 3 遍，无竞态）。
  - cluster 包：`TestSnapshotDeliveryAndCatchup`、`TestPhaseGuards`、
    `TestStaleRouteAndUnknownTarget`、`TestResetShardWipesState`、
    `TestSnapshotPhaseWritesNoHole`、`TestAuditRequiresFullDelivery`
  - httpapi 包：`TestAcceptanceLifecycle`、`TestPrimaryDownRejectsWrites`、
    `TestConcurrentNoDualPrimary`
- 覆盖率：cluster 70.6%，httpapi 72.7%（未覆盖部分主要是错误分支与 panic 恢复）。
- 真实起服务 + `scripts/demo.sh` 走查：陈旧 v1 写 `HTTP 409 stale_route`；
  目标断连/滞后时切换两次 `409`；切换响应 `old=node-a → new=node-b,
  v1→v2, switch_seq=5`；重复 `k-sw` 返回 `idempotent_replay:true` 且
  `switch_seq` 不变；最终审计 `healthy=true`，各项违规计数为 0，
  `writes_missing_on_new_primary=0`，写日志 1–5 为 node-a/v1、6–7 为 node-b/v2。

### 实现过程中发现并修复的一个真实缺陷（已加回归测试）

首版用“增量水位线 `caught_up_seq`”判断新主进度。SNAPSHOT 阶段确认的写
（`seq > start_seq`）既不属于全量快照，实时流式又只在进入 CATCHUP 后生效；
随后 CATCHUP 的一条写把水位线顶过了那条快照期写，导致它从未投递到新主。
真实起服务走查时在写日志里观察到该写 `delivered_to` 缺 node-b。
修复：投递与否一律以**每条写的 `delivered_to` 实际集合**为准
（快照完成时顺带补快照期写、catchup 按缺口投递、切换门控数缺失写），
并新增审计项 `writes_missing_on_new_primary` 与回归测试
`TestSnapshotPhaseWritesNoHole`。修复后重跑全部测试与端到端走查通过。

## 目录结构

```
go.mod
cmd/shardmigrator/main.go          入口：装配集群 + HTTP server + 优雅退出
internal/cluster/cluster.go        核心状态机：阶段、序号、投递、幂等、审计
internal/httpapi/server.go         net/http 路由、JSON 信封、错误码映射
internal/cluster/*_test.go         核心单元 + 回归测试
internal/httpapi/*_test.go         端到端验收 + 并发防双主测试
scripts/demo.sh                    一键 HTTP 演示
```

## 未完成项 / 刻意的简化（如实说明）

- **纯内存、单进程**：节点是逻辑角色而非真实进程/网络，断连是一个布尔故障
  开关；没有真实 Raft/Paxos、磁盘 WAL 或崩溃恢复。它模拟的是协议的**安全
  语义**，不是生产级复制。
- **固定两节点、启动即内置一个分片**：未暴露“创建节点/分片”的 HTTP 接口
  （核心库支持多节点多分片，并有 `reset`；仅未加对应 HTTP 建集群端点）。
- **无鉴权/TLS、无分页/限流、无前端界面**：按“纯后端 + HTTP 接口”要求实现。
- **时间用真实时钟**仅用于展示 `confirmed_at`，正确性不依赖时钟，无时钟偏移处理。
- 路由版本用整数自增；未实现版本回退、多分片原子编排或跨分片事务。
