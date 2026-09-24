# shardmig — 单主分片迁移切换协议模拟器（Go, net/http, 纯后端）

模拟一个单主分片从节点 **A（epoch 1）** 在线迁移到节点 **B（epoch 2）** 的完整过程，
覆盖三个阶段：**快照 snapshot → 增量追赶 catch-up → 路由版本切换 cutover**。

每次写入带全局单调序号 `seq`；切换点前后，同一时刻只允许唯一的主节点确认新写。
系统提供 HTTP 接口，支持注入**断连**、**重复控制消息**和**提交后响应丢失**，
并通过 `/verify` 自动审计四项不变量。

仅依赖 Go 标准库（`net/http`），无任何第三方依赖。

---

## 1. 协议与设计

### 路由版本（epoch）与单主

- 控制面 `core.Cluster` 用一把互斥锁线性化所有“谁能确认写”的判定。
- 节点只缓存“我最后被告知的 epoch”，不做权威判定。每次 `/append` 都在锁内重新裁决：

  ```
  链路在线？ → 集群未冻结(switching)？ → 客户端 epoch == 当前 epoch？ → 该节点是当前 primary？
  ```

  任一不满足即拒绝（502 / 503 / 409 stale_epoch / 403 not_primary）。

- 切换提交时 `epoch++`、`primary: A→B`。旧主 A 即便继续发写，也会因
  **epoch 过期（409）** 被拒；A 即使伪造新 epoch 2，也会因 **不是 primary（403）** 被拒。
  因此切换点前后不可能出现两个主同时确认。

### 三个阶段

| 阶段 | 控制消息 | 语义 | 期间能否写 |
|---|---|---|---|
| `none` | — | 初始，A 是 epoch 1 主 | A 可写 |
| `snapshot` | `begin_snapshot` | 记录快照点 `snapshot_seq=lastSeq` | A 可写 |
| `snapshot→catchup` | `complete_snapshot` | 通过 HTTP 把 A 上 `[1..snapshot_seq]` 拉取并安装到 B | A 可写 |
| `catchup` | `catch_up`（可重复） | 把快照点之后的新增量传到 B，返回 `lag`，直到 `lag=0` | A 可写 |
| `switching` | `prepare_cutover` | 要求 `lag=0`，随后**冻结所有写** | 全部 503 |
| `done` | `commit_cutover` | epoch+1，B 成为唯一主 | 仅 B(epoch 新) 可写 |

切换要求 B 完全追平（`prepare_cutover` 校验 `a_last_seq == b_last_seq`），
因此不会有已确认写被遗留在退役主 A 上。

### 幂等 / exactly-once 控制消息

- 每条控制消息可带客户端生成的 `command_id`。控制面缓存**首次成功结果**；
  重复投递同一 `command_id` 直接回放原结果（响应含 `"replayed": true`），状态不二次推进。
- **失败不缓存**：例如 B 断连时 `commit_cutover` 返回 502，恢复后用同一命令 ID 重试会真正执行。

### 写入幂等

- `/append` 以 `key` 去重：同一 key 重复写返回已存在记录且 `"deduplicated": true`，不分配新 seq。
  这用于验证“提交成功但响应丢失”后客户端盲目重试不会产生重复写。

### 故障注入

- `POST /nodes/{A|B}/link {"up":false}` —— 模拟控制器↔节点分区（确定性，非随机）。
  断链节点既不能确认写，也收不到快照/增量批次。
- `POST /nodes/A/drop-append {"drop":true}` —— 下一次写在锁内提交后丢弃响应（返回 500），
  模拟“已提交但客户端没收到”。该故障一次性触发后自动解除。

### 四项不变量（`GET /verify`）

1. `sequences_gap_free` —— A 上序号 1..N 无空洞。
2. `all_confirmed_writes_present` —— 切换后 B 持有全部已确认写（无已确认写丢失）。
3. `epoch_provenance` —— A 上只有 epoch 1/A 确认的写；B 的新写只能是当前 epoch、由 B 确认；
   传输过去的写保留 epoch 1 来源。
4. `no_dual_primary_confirmation` —— 任何 key 不会被两个不同主确认（无双主）。

### 进程结构

三个**真实 TCP HTTP 服务**（均在 loopback）：

- 一个控制器服务（客户端入口，默认 `127.0.0.1:8080`）
- 节点 A、节点 B 各一个 HTTP 服务（临时端口，地址见启动日志/`/status`）

快照与增量通过**真实 HTTP** 从 A 的 `/internal/log` 拉取、POST 到 B 的 `/internal/install` 安装，
再由控制器幂等提交阶段推进。

---

## 2. 依赖与启动

### 依赖

- Go **1.23+**（开发与验证使用 `go1.23.4 linux/amd64`）。
- 仅 Go 标准库，**零第三方依赖**。
- 依赖锁定：`go.mod` 中 `go 1.23` + `toolchain go1.23.4`；
  因无外部模块，没有也不需要 `go.sum`（`go mod tidy` 不产生任何条目）。

### 构建 / 测试 / 运行

```bash
# 编译检查
go build ./...
go vet ./...

# 运行全部自动化测试（含 -race 竞态检测）
go test -race ./...

# 启动 HTTP 服务（控制器固定端口；A/B 用临时端口，见启动日志）
go run ./cmd/server -addr 127.0.0.1:8080

# 一键跑端到端验收场景（内置在进程内通过真实 HTTP 自测）
go run ./cmd/demo

# 或：启动服务后，用 curl 走完整流程
examples/curl-walkthrough.sh
```

启动后输出形如：

```
shard migration simulator started
  controller : http://127.0.0.1:8080
  node A     : http://127.0.0.1:40725
  node B     : http://127.0.0.1:45791
```

---

## 3. HTTP 接口

### 数据面（控制器）

| 方法 路径 | 说明 |
|---|---|
| `GET /healthz` | 存活检查 |
| `GET /status` | 权威状态：epoch、primary、phase、snapshot_seq、last_seq、链路、各节点视图 |
| `GET /route` | 当前路由版本与主节点 |
| `POST /append` | 客户端写：`{"node","epoch","key","value"}` |
| `GET /verify` | 四项不变量审计 |
| `POST /control/reset` | 重置到初始状态（演示/测试用） |

### 迁移控制面

| 方法 路径 | body |
|---|---|
| `POST /migration/begin_snapshot` | `{"command_id":"..."}` |
| `POST /migration/complete_snapshot` | `{"command_id":"..."}`（经 HTTP A→B 传快照） |
| `POST /migration/catch_up` | `{"command_id":"..."}`（经 HTTP A→B 传增量，返回 lag） |
| `POST /migration/prepare_cutover` | `{"command_id":"..."}`（lag=0 才允许，随后冻结） |
| `POST /migration/commit_cutover` | `{"command_id":"..."}`（epoch+1，主切到 B） |

### 故障注入

| 方法 路径 | body |
|---|---|
| `POST /nodes/{A\|B}/link` | `{"up": true/false}` |
| `POST /nodes/{A\|B}/drop-append` | `{"drop": true/false}` |

### 节点服务（A / B）

| 方法 路径 | 说明 |
|---|---|
| `GET /healthz`、`GET /info`、`GET /writes` | 节点健康 / epoch 视图 / 已确认写日志 |
| `POST /internal/log?after=&upto=` | 控制器拉取日志区间（upto=-1 表示无上界） |
| `POST /internal/install` | 控制器安装快照/增量批次（按 key 去重） |

### 错误码

| HTTP | code | 含义 |
|---|---|---|
| 409 | `stale_epoch` | 用旧路由版本写到已切换的集群 |
| 403 | `not_primary` | epoch 对但该节点不是主 |
| 503 | `cluster_frozen` | switching 冻结窗口内写 |
| 502 | `node_down` | 目标节点断连（写或数据传输/epoch 推送失败） |
| 409 | `bad_phase` | 阶段顺序非法（如未 catch-up 就 cutover） |
| 500 | `response_dropped_after_commit` | 已提交但响应被注入丢弃 |

---

## 4. 请求样例（curl）

```bash
B=http://127.0.0.1:8080

# 初始写：A 以 epoch 1 确认，返回 seq
curl -s -XPOST $B/append -d '{"node":"A","epoch":1,"key":"k1","value":"v1"}'
# {"seq":1,"epoch":1,"primary":"A","key":"k1","value":"v1","deduplicated":false}

# 阶段1：快照
curl -s -XPOST $B/migration/begin_snapshot      -d '{"command_id":"b"}'
curl -s -XPOST $B/migration/complete_snapshot   -d '{"command_id":"s"}'

# 阶段2：增量追赶（有新写时重复调用直到 lag=0）
curl -s -XPOST $B/migration/catch_up            -d '{"command_id":"c1"}'
curl -s -XPOST $B/migration/catch_up            -d '{"command_id":"c1"}'   # 重复→replayed:true

# 断连：断开 B，快照/切换会 502；恢复后同命令 ID 重试成功
curl -s -XPOST $B/nodes/B/link -d '{"up":false}'
curl -s -XPOST $B/nodes/B/link -d '{"up":true}'

# 阶段3：冻结并切换
curl -s -XPOST $B/migration/prepare_cutover     -d '{"command_id":"p"}'
curl -s -XPOST $B/migration/commit_cutover      -d '{"command_id":"co"}'   # epoch→2, primary→B

# 切换后：A 被 fence，只有 B 能确认
curl -s -i -XPOST $B/append -d '{"node":"A","epoch":1,"key":"x","value":"y"}'  # 409 stale_epoch
curl -s -i -XPOST $B/append -d '{"node":"A","epoch":2,"key":"x","value":"y"}'  # 403 not_primary
curl -s -XPOST $B/append -d '{"node":"B","epoch":2,"key":"k2","value":"v2"}'   # 200

# 不变量审计
curl -s $B/verify
```

完整可执行脚本：[`examples/curl-walkthrough.sh`](examples/curl-walkthrough.sh)，
其一次真实运行的输出留档：[`examples/expected-output.txt`](examples/expected-output.txt)。

---

## 5. 自动化测试

```bash
go test -race -count=1 ./...
```

测试分两层：

- `core/cluster_test.go`（状态机单元测试）：阶段顺序、切换后四种路由组合的 fence、
  命令 ID 幂等缓存、以及直接构造“双主确认”状态验证 `/verify` 能抓到。
- `api/server_test.go`（**真实 HTTP 端到端**，启动 3 个临时 TCP 服务）：
  - `TestHappyPathLifecycle`：每个阶段都插入写入，完成切换，无写丢失；
  - `TestDisconnectInEveryPhase`：在 snapshot / catch-up / switching 各阶段断连，
    断言危险推进被阻止、恢复后可续、失败提交不推进 epoch；
  - `TestDuplicatedControlMessages`：重复控制消息回放原结果、不二次推进；失败不缓存可重试；
  - `TestAppendResponseLostRetried`：提交后响应丢失，盲目重试恰好只保留一条写且 seq 不重复；
  - `TestNoDualPrimaryAcrossCutover`：在 commit 栅栏处对 A/B 并发写 + 并发提交（`-race`），
    断言切换窗口存在拒绝、共享 key 最终仅由 B 确认、绝无双主；
  - `TestSnapshotCatchupContents`：快照/增量传输边界正确、B 上按全局 seq 有序。

---

## 6. 实际运行结果（如实记录）

以下为在本环境（Ubuntu 24.04, go1.23.4 linux/amd64）上的真实执行结果。

### `go test -race ./...`

```
ok  	shardmig/api          (含 6 个端到端测试)
ok  	shardmig/core         (含 4 个状态机测试)
?   	shardmig/cmd/server   [no test files]
```

为排查并发切换用例的时序敏感性，曾以 `go test -race -count=20 ./...` 连跑 20 轮，全部通过、
无 data race。（开发过程中该用例首版因“并发尝试全部落在冻结窗口、commit 尚未完成”出现过一次
0 确认的断言失败，已将用例改为在 burst 与 commit 收敛后用新主 B 对共享 key 做一次确定化确认，
属于测试夹具修正；服务端 fence 行为本身符合预期。）

### `go run ./cmd/demo`

20 步验收场景全部通过：每阶段写入、每阶段断连、重复控制消息、丢响应重试，
最终 `/verify` 四项 `PASS`，末态 `epoch=2 primary=B last_seq=7`，B 持有 7 条、A 6 条。

### curl 全流程

`examples/expected-output.txt` 为对真实运行中的服务执行 `curl-walkthrough.sh` 的留档，
关键节点：快照期 B 断连 → 502；重复 catch_up → `replayed:true`；丢响应 → 500 后重试
`deduplicated:true`；冻结期写 → 503；切换提交时 B 断连 → 502 且 epoch 不变；
切换后 A → 409 / 403，B(epoch2) 写成功；`/verify` 全 PASS。

开发中另发现并修复的两个真实缺陷：

1. **日志区间上界歧义**：`LogRange` 原用 `upTo<=0` 表示无上界，导致“空快照点(snapshot_seq=0)”
   会把快照点之后的写错误并入快照批次。已改为 `upTo=-1` 表示无上界，并由
   `TestSnapshotCatchupContents` 与状态机测试覆盖。
2. **HTTP 传输计数**：快照/增量先经 HTTP 落到 B，再由控制器幂等提交阶段；初版 core 层
   `installed` 重复计为 0。已让 API 层把 HTTP 实际安装计数传入提交结果。

### 未完成项 / 已知简化

- **单进程模拟**：A、B、控制器是同一进程内的三个 HTTP 服务，共享一把权威锁；
  “断连”是确定性的链路表标志，而非真实 iptables/网络分区。这是模拟器的有意简化，
  足以线性化验证“单主确认”语义，但不是分布式共识实现（无 Raft/多数派/持久化 WAL）。
- **内存态**：数据不持久化，进程重启即丢失；`/control/reset` 用于演示复位。
- **客户端路由发现简化**：`/append` 仍打统一控制器入口，由 body 中的 `node/epoch` 表达
  “客户端按某路由版本写到某节点”；没有实现真正的代理转发与自动重定向（旧客户端收到 409 后
  需自行从 `/route` 刷新路由再重试，README 样例演示了这一刷新动作）。
- 未做鉴权、TLS 与限流（纯本地验收用途）。

---

## 7. 目录结构

```
.
├── go.mod                      # 模块声明 + 版本/工具链锁定（零第三方依赖）
├── core/
│   ├── cluster.go              # 权威状态机：epoch/阶段/写线性化/幂等/校验
│   └── cluster_test.go         # 状态机单元测试
├── api/
│   ├── server.go               # 控制器 + A/B 三个真实 HTTP 服务
│   └── server_test.go          # 真实 HTTP 端到端测试（断连/重复/丢响应/并发切换）
├── cmd/
│   ├── server/main.go          # HTTP 服务入口
│   └── demo/main.go            # 一键端到端验收场景
└── examples/
    ├── curl-walkthrough.sh     # curl 完整流程脚本
    └── expected-output.txt     # 该脚本一次真实运行的留档
```
